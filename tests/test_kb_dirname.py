"""EXOMEM_KB_DIRNAME — the governed-folder name is configurable (default "Knowledge Base").

Safety net for the "un-hardcode the KB folder name" refactor: with EXOMEM_KB_DIRNAME
set, the engine must init, resolve, govern, and *wikilink* against that folder name
instead of the literal "Knowledge Base". These assertions define "done"; the rest of the
suite (which runs with no override) guards the default path.
"""

from __future__ import annotations

from pathlib import Path


def _min_vault(root: Path, kb: str) -> Path:
    (root / kb / "_Schema").mkdir(parents=True)
    (root / kb / "_Schema" / "SKILL.md").write_text("---\nname: exomem\n---\n", encoding="utf-8")
    return root


def test_kb_dirname_default(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_KB_DIRNAME", raising=False)
    from exomem import kbdir

    assert kbdir.kb_dirname() == "Knowledge Base"
    assert kbdir.kb_prefix() == "Knowledge Base/"


def test_kb_dirname_override(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    from exomem import kbdir

    assert kbdir.kb_dirname() == "Brain"
    assert kbdir.kb_prefix() == "Brain/"


def test_init_creates_custom_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    from exomem import init as init_module

    init_module.init_vault(tmp_path)
    assert (tmp_path / "Brain" / "_Schema" / "SKILL.md").exists()
    assert not (tmp_path / "Knowledge Base").exists()


def test_resolve_and_kb_root_custom(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    _min_vault(tmp_path, "Brain")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    from exomem import vault as vault_module

    assert vault_module.resolve_vault() == tmp_path
    assert vault_module.kb_root(tmp_path) == tmp_path / "Brain"


def test_access_config_path_custom(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    from exomem import access as access_module

    assert access_module.access_config_path(tmp_path) == tmp_path / "Brain" / "_access.yaml"


def test_access_tier_strips_custom_prefix(monkeypatch, tmp_path: Path) -> None:
    """A path written with the custom KB prefix must resolve tiers the same as a
    KB-relative one (the `_kb_relative` strip must use the configured name)."""
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    _min_vault(tmp_path, "Brain")
    (tmp_path / "Brain" / "_access.yaml").write_text("readonly:\n- Reference\n", encoding="utf-8")
    from exomem import access as access_module

    assert access_module.access_tier(tmp_path, "Brain/Reference/x.md") == access_module.TIER_READONLY
    assert access_module.access_tier(tmp_path, "Reference/x.md") == access_module.TIER_READONLY


def test_normalize_wikilink_custom_prefix(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    _min_vault(tmp_path, "Brain")
    from exomem import vault as vault_module

    # An unresolvable KB-relative target canonicalizes under the CUSTOM prefix.
    canonical, _warn = vault_module.normalize_wikilink("Notes/Insights/foo", tmp_path, strict=False)
    assert canonical == "Brain/Notes/Insights/foo"

    # A target already carrying the custom prefix is preserved.
    canonical2, _warn2 = vault_module.normalize_wikilink("Brain/Notes/Insights/foo", tmp_path, strict=False)
    assert canonical2 == "Brain/Notes/Insights/foo"
