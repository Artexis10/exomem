"""access: per-path tiers from _access.yaml layered over built-in defaults."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from exomem import access


def _write_cfg(vault: Path, text: str) -> Path:
    p = vault / "Knowledge Base" / "_access.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_default_tiers_without_config(vault: Path) -> None:
    assert access.access_tier(vault, "Knowledge Base/Notes/Insights/foo.md") == access.TIER_READ_WRITE
    assert access.access_tier(vault, "Knowledge Base/Sources/Articles/x.md") == access.TIER_APPEND_ONLY
    assert access.access_tier(vault, "Knowledge Base/Evidence/Legal/x.pdf") == access.TIER_APPEND_ONLY
    # No config → nothing is excluded, everything (bar excluded) is indexable.
    assert access.is_indexable(vault, "Knowledge Base/Notes/x.md") is True
    assert access.writable_reason(vault, "Knowledge Base/Notes/x.md") is None


def test_append_only_tier_is_case_insensitive() -> None:
    """On a case-insensitive filesystem (Windows/macOS) an uppercase `SOURCES/`
    aliases the real `Sources/` on disk, so the append-only guard must match
    regardless of case — else raw Sources/Evidence are editable via the alias.
    Regression for the audit's confirmed HIGH."""
    from exomem import vault as vault_module

    for variant in ("SOURCES", "sources", "Sources", "SoUrCeS"):
        assert vault_module.in_append_only_tree(f"Knowledge Base/{variant}/Articles/x.md") is not None, variant
    for variant in ("EVIDENCE", "evidence", "Evidence"):
        assert vault_module.in_append_only_tree(f"Knowledge Base/{variant}/Legal/x.pdf") is not None, variant
    # access_tier (the batch-write backstop's source of truth) agrees.
    assert access.access_tier(Path("/vault"), "Knowledge Base/SOURCES/Articles/x.md") == access.TIER_APPEND_ONLY
    assert access.access_tier(Path("/vault"), "Knowledge Base/EVIDENCE/x.pdf") == access.TIER_APPEND_ONLY
    # A folder that merely starts with the reserved name is NOT append-only.
    assert vault_module.in_append_only_tree("Knowledge Base/Sources-of-truth/x.md") is None


def test_readonly_from_config(vault: Path) -> None:
    _write_cfg(vault, "readonly:\n  - Reference\n  - Library\n")
    assert access.access_tier(vault, "Knowledge Base/Reference/Strategy.md") == access.TIER_READONLY
    # nested + KB-stripped form both resolve to the same tier
    assert access.access_tier(vault, "Reference/sub/deep.md") == access.TIER_READONLY
    assert access.writable_reason(vault, "Knowledge Base/Reference/Strategy.md") is not None
    # readonly is still findable
    assert access.is_indexable(vault, "Knowledge Base/Reference/Strategy.md") is True
    # a non-listed folder stays read-write
    assert access.access_tier(vault, "Knowledge Base/Notes/x.md") == access.TIER_READ_WRITE


def test_excluded_hides_and_blocks(vault: Path) -> None:
    _write_cfg(vault, "excluded:\n  - Private\n")
    assert access.access_tier(vault, "Knowledge Base/Private/secret.md") == access.TIER_EXCLUDED
    assert access.is_indexable(vault, "Knowledge Base/Private/secret.md") is False
    assert access.is_indexable(vault, "Knowledge Base/Notes/x.md") is True
    assert access.writable_reason(vault, "Knowledge Base/Private/secret.md") is not None


def test_excluded_outranks_readonly(vault: Path) -> None:
    _write_cfg(vault, "readonly:\n  - Shared\nexcluded:\n  - Shared/Private\n")
    assert access.access_tier(vault, "Shared/notes.md") == access.TIER_READONLY
    assert access.access_tier(vault, "Shared/Private/secret.md") == access.TIER_EXCLUDED


def test_config_live_reloads_on_mtime_change(vault: Path) -> None:
    p = _write_cfg(vault, "readonly:\n  - Reference\n")
    assert access.access_tier(vault, "Reference/AI.md") == access.TIER_READONLY
    future = time.time() + 2
    p.write_text("readonly: []\n", encoding="utf-8")
    os.utime(p, (future, future))
    assert access.access_tier(vault, "Reference/AI.md") == access.TIER_READ_WRITE


def test_batch_write_refuses_readonly_tree(vault: Path) -> None:
    # The central enforcement: a content write into a readonly tree is refused.
    from exomem.vault import PlannedWrite, batch_atomic_write
    _write_cfg(vault, "readonly:\n  - Reference\n")
    blocked = vault / "Knowledge Base" / "Reference" / "x.md"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="WRITE_REFUSED"):
        batch_atomic_write([PlannedWrite(path=blocked, content="hi")], vault_root=vault)
    # a normal path still writes fine (no-op guard for read-write tiers)
    ok = vault / "Knowledge Base" / "Notes" / "ok.md"
    ok.parent.mkdir(parents=True, exist_ok=True)
    batch_atomic_write([PlannedWrite(path=ok, content="hi")], vault_root=vault)
    assert ok.read_text(encoding="utf-8") == "hi"


def test_find_hides_excluded_tree(vault: Path) -> None:
    from exomem import find as find_module
    secret = vault / "Knowledge Base" / "Private" / "secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("---\ntype: source\n---\nzzqqxx unique marker\n", encoding="utf-8")
    find_module.clear_cache()
    # control: surfaced when nothing excludes it
    assert any("secret" in h.path.lower() for h in find_module.find(vault, query="zzqqxx"))
    # exclude Private/ → the page disappears from results
    _write_cfg(vault, "excluded:\n  - Private\n")
    find_module.clear_cache()
    assert not any("secret" in h.path.lower() for h in find_module.find(vault, query="zzqqxx"))
