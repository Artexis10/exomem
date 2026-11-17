"""A `root` vault is written with links in Obsidian's shortest unique form, and every written link resolves.

The journey runs the tool entry points against a vault bound as `root`. After each write, every link in the
pages the writers touched is resolved the way Obsidian does, over every Markdown file the vault holds: the
managed areas, the trash and archive folders, and content outside the areas. A link that suffix-matches a
second file, or no file, is a broken link for the person who opens the vault.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401

from exomem import commands, obsidian_links
from exomem import schema as schema_module
from exomem import vault as vault_module
from exomem.governance.principal import library_scope
from exomem.vault_store import write
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
NOTES = "Notes"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"


@pytest.fixture
def root_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture's managed folder as the vault root, less its `Notes/` tree, bound as the served vault."""
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns(NOTES))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    bind_root_vault(dest, monkeypatch)
    initialize_vault_state_offline(dest, source="root links fixture")
    return dest


def _write_markdown(vault_root: Path, relative: str, text: str = "Held content.\n") -> None:
    path = vault_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _obsidian_sees(vault_root: Path) -> dict[str, list[str]]:
    """The basename index over every Markdown file on disk, as Obsidian lists them."""
    found = (path.relative_to(vault_root).as_posix() for path in vault_root.rglob("*.md"))
    return obsidian_links.build_index(found)


def _link_texts(text: str) -> list[str]:
    """The page part of every wikilink in `text` that names a page, in order; a folder link names none."""
    pages = (vault_module.split_wikilink_target(match.group(0))[0] for match in vault_module.find_body_wikilinks(text))
    return [page for page in pages if not page.endswith("/")]


def _resolved(vault_root: Path, relative: str, *, besides: frozenset[str] = frozenset()) -> list[str]:
    """Where each link in the page at `relative` lands under Obsidian's rule, apart from the links in `besides`.

    A link that is ambiguous or broken fails. `besides` holds the links the page carried before the write.
    """
    index = obsidian_links.IndexResolver(_obsidian_sees(vault_root))
    landed = []
    for link in _link_texts((vault_root / relative).read_text(encoding="utf-8")):
        if link in besides:
            continue
        match = index.resolve(link)
        assert match.path is not None, f"{relative}: [[{link}]] is {match.outcome} ({match.paths})"
        landed.append(match.path)
    return landed


def _remember(vault_root: Path, title: str, note_type: str, content: str = UNIT) -> str:
    with library_scope():
        created = commands.op_remember(
            vault_root, content=content, status="draft", title=title, note_type=note_type
        )
    return created["path"]


def test_a_note_links_a_unique_page_by_its_name_and_a_homonym_by_its_shortest_path(
    root_vault: Path,
) -> None:
    """Two areas share a name, a trash folder shares another, and a fourth page is the only page of its name."""
    _write_markdown(root_vault, "_trash/restart-order.md")
    checklist_insight = _remember(root_vault, "Pump seal checklist", "insight")
    checklist_pattern = _remember(root_vault, "Pump seal checklist", "pattern")
    trashed_twin = _remember(root_vault, "Restart order", "insight")
    only = _remember(root_vault, "Valve inventory", "insight")
    assert Path(checklist_insight).name == Path(checklist_pattern).name

    pages = [checklist_insight, checklist_pattern, trashed_twin, only]
    body = " ".join(f"[[{page.removesuffix('.md')}]]" for page in pages)
    note = _remember(root_vault, "Maintenance summary", "insight", f"{UNIT}\n{body}\n")

    texts = _link_texts((root_vault / note).read_text(encoding="utf-8"))
    assert texts == [
        f"{Path(checklist_insight).parent.name}/pump-seal-checklist",
        f"{Path(checklist_pattern).parent.name}/pump-seal-checklist",
        f"{Path(trashed_twin).parent.name}/restart-order",
        "valve-inventory",
    ]
    assert _resolved(root_vault, note) == pages


def test_a_source_capture_links_its_page_in_the_sources_index(root_vault: Path) -> None:
    """The new Source is a bare name in the index when its name is unique, and its link resolves to it alone."""
    schema = schema_module.load_source_schema(root_vault)
    _write_markdown(root_vault, "Archive/2026-05-18-older-capture.md")
    page = "Sources/index.md"
    before = frozenset(_link_texts((root_vault / page).read_text(encoding="utf-8")))

    saved = commands.op_capture_source(
        root_vault,
        schema,
        title="Pump maintenance handbook",
        content="Replace the seal every year.",
        source_kind="article",
        url="https://example.com/pump-handbook",
    )["source"]

    assert _resolved(root_vault, page, besides=before) == [saved["path"]]
    assert Path(saved["path"]).stem in _link_texts((root_vault / page).read_text(encoding="utf-8"))


def test_a_link_to_an_entity_resolves_after_the_entity_is_written(root_vault: Path) -> None:
    """The entity writer renders connections against its own snapshot, which holds the page it is creating."""
    with library_scope():
        person = commands.op_connect_memory(
            root_vault,
            operation="create-entity",
            entity_type="person",
            name="Mira Okafor",
            summary="Maintains the pump fleet.",
            why_in_kb="She owns the maintenance runbook.",
        )
        note = _remember(root_vault, "Runbook owner", "insight")
        commands.op_connect_memory(
            root_vault,
            operation="create-entity",
            entity_type="organization",
            name="Pump Works",
            summary="Builds the pumps.",
            why_in_kb="It supplies the seals.",
            connections=[person["path"].removesuffix(".md"), note.removesuffix(".md")],
        )

    organization = next(path for path in (root_vault / "Entities").rglob("Pump Works.md"))
    landed = _resolved(root_vault, organization.relative_to(root_vault).as_posix())
    assert set(landed) == {person["path"], note}


def test_a_store_mode_vault_counts_the_machinery_files_it_stores_and_the_ones_it_does_not(
    stored, writer  # noqa: F811
) -> None:
    """A machinery folder holds stored pages and loose files; the group counts both and no content page."""
    write.commit(writer, put("Knowledge Base/_trash/Gamma.md", b"# Gamma\n"))
    _write_markdown(stored, "Knowledge Base/_archive/2026/Delta.md")

    found = set(vault_module._machinery_markdown(stored))

    assert {"Knowledge Base/_trash/Gamma.md", "Knowledge Base/_archive/2026/Delta.md"} <= found
    assert ALPHA not in found
    assert all(path.lower().endswith(".md") for path in found)
