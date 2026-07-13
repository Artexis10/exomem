from __future__ import annotations

from pathlib import Path

from exomem import vault


def test_wikilink_resolver_from_entries_matches_disk_resolution(tmp_path: Path) -> None:
    entries = (
        ("Knowledge Base/Notes/one.md", "Display One"),
        ("Knowledge Base/Elsewhere/shared.md", "Shared A"),
        ("Knowledge Base/Notes/shared.md", "Shared B"),
    )
    for rel_path, title in entries:
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\n---\n", encoding="utf-8")

    disk = vault.WikilinkResolver(tmp_path)
    snapshot = vault.WikilinkResolver.from_entries(tmp_path, entries)
    targets = (
        "Knowledge Base/Notes/one",
        "Notes/one",
        "one",
        "display one",
        "shared",
        "missing",
    )

    assert [
        vault.normalize_wikilink(target, tmp_path, resolver=snapshot)
        for target in targets
    ] == [vault.normalize_wikilink(target, tmp_path, resolver=disk) for target in targets]

    snapshot.add_pending("Knowledge Base/Notes/pending", title="Pending title")
    assert vault.normalize_wikilink("pending", tmp_path, resolver=snapshot) == (
        "Knowledge Base/Notes/pending",
        None,
    )
    assert vault.normalize_wikilink("PENDING TITLE", tmp_path, resolver=snapshot) == (
        "Knowledge Base/Notes/pending",
        None,
    )


def test_wikilink_resolver_from_entries_performs_no_io(
    tmp_path: Path, monkeypatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("from_entries must not walk or read the vault")

    monkeypatch.setattr(vault, "walk_vault_md", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)

    resolver = vault.WikilinkResolver.from_entries(
        tmp_path, (("Knowledge Base/Notes/one.md", "One"),)
    )

    assert resolver.full_paths == {"Knowledge Base/Notes/one"}
