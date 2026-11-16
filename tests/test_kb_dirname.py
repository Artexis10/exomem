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
    # The shipped contract sits outside the note namespace, so its path does not
    # follow the note directory that `EXOMEM_KB_DIRNAME` renames.
    assert (tmp_path / ".exomem" / "schema" / "SKILL.md").exists()
    assert (tmp_path / "Brain" / "_Schema" / "project-keys.yaml").exists()
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


# The managed_* helpers are the only code that knows how a managed-root-relative path
# becomes a vault-relative path and back. Each test names the call-site failure it guards.


def test_managed_join_and_split_round_trip(monkeypatch) -> None:
    from exomem import kbdir

    for name in (None, "Brain"):
        if name is None:
            monkeypatch.delenv("EXOMEM_KB_DIRNAME", raising=False)
        else:
            monkeypatch.setenv("EXOMEM_KB_DIRNAME", name)
        for rel in ("", "Sources/x.md", "Sources/", "a b/Knowledge Base/y.md"):
            joined = kbdir.managed_join(rel)
            assert joined == kbdir.kb_prefix() + rel
            assert kbdir.managed_split(joined) == rel
            assert kbdir.managed_splitter()(joined) == rel


def test_managed_split_is_none_outside_the_managed_root(monkeypatch) -> None:
    from exomem import kbdir

    monkeypatch.delenv("EXOMEM_KB_DIRNAME", raising=False)
    for outside in ("Reference/x.md", "Knowledge Base", "Knowledge Basement/x.md", "knowledge base/x.md", ""):
        assert kbdir.managed_split(outside) is None
        assert kbdir.managed_splitter()(outside) is None


def test_managed_split_with_a_key_compares_the_name_loosely(monkeypatch) -> None:
    from exomem import kbdir

    monkeypatch.delenv("EXOMEM_KB_DIRNAME", raising=False)
    assert kbdir.managed_split("KNOWLEDGE base/Sources/X.md", key=str.casefold) == "Sources/X.md"
    # The key reads the first segment only: the bare name has no remainder, and a longer name differs.
    assert kbdir.managed_split("knowledge base", key=str.casefold) is None
    assert kbdir.managed_split("knowledge basement/x.md", key=str.casefold) is None


def test_managed_split_parts_returns_the_components_below_the_root(monkeypatch) -> None:
    from exomem import kbdir

    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    assert kbdir.managed_split_parts(("Brain", "Records", "x.md")) == ("Records", "x.md")
    assert kbdir.managed_split_parts(("Brain",)) == ()
    assert kbdir.managed_split_parts(("brain", "x.md")) is None
    assert kbdir.managed_split_parts(("brain", "x.md"), key=str.casefold) == ("x.md",)
    assert kbdir.managed_split_parts(()) is None
    assert kbdir.is_managed_root_name("Brain") and not kbdir.is_managed_root_name("brain")
    assert kbdir.is_managed_root_name("brain", key=str.casefold)


def test_managed_form_and_relative_only_touch_the_leading_root(monkeypatch) -> None:
    from exomem import kbdir

    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    assert kbdir.managed_form("Sources/x.md") == "Brain/Sources/x.md"
    assert kbdir.managed_form("Brain/Sources/x.md") == "Brain/Sources/x.md"
    assert kbdir.managed_form("Brainy/x.md") == "Brain/Brainy/x.md"
    assert kbdir.managed_relative("Brain/Sources/x.md") == "Sources/x.md"
    assert kbdir.managed_relative("Sources/Brain/x.md") == "Sources/Brain/x.md"
    assert kbdir.is_in_managed_root("Brain") and kbdir.is_in_managed_root("Brain/x.md")
    assert not kbdir.is_in_managed_root("Brainy/x.md")


def test_kb_relative_form_normalises_separators_before_adding_the_root(monkeypatch) -> None:
    """The hosted guard and the executors both call this, so it must give a single answer."""
    from exomem import kbdir

    monkeypatch.delenv("EXOMEM_KB_DIRNAME", raising=False)
    assert kbdir.kb_relative_form("  \\Sources\\x.md ") == "Knowledge Base/Sources/x.md"
    assert kbdir.kb_relative_form("/Knowledge Base/Sources/x.md") == "Knowledge Base/Sources/x.md"
    assert kbdir.kb_relative_form("knowledge base/x.md") == "Knowledge Base/knowledge base/x.md"


def test_managed_respell_restores_only_a_case_difference(monkeypatch) -> None:
    from exomem import kbdir

    monkeypatch.delenv("EXOMEM_KB_DIRNAME", raising=False)
    assert kbdir.managed_respell("knowledge base/Sources/x.md") == "Knowledge Base/Sources/x.md"
    assert kbdir.managed_respell("KNOWLEDGE BASE") == "Knowledge Base"
    assert kbdir.managed_respell("Knowledge Base/x.md") == "Knowledge Base/x.md"
    assert kbdir.managed_respell("Reference/knowledge base/x.md") == "Reference/knowledge base/x.md"


def test_managed_root_helpers_follow_the_configured_name(monkeypatch, tmp_path: Path) -> None:
    from exomem import kbdir

    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    assert kbdir.managed_root(tmp_path) == tmp_path / "Brain"
    assert kbdir.managed_root_rel() == "Brain"
