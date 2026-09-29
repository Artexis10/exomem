"""A small invented vault for the conversation-aware activation tests.

Entities only, so every anchor is an exact-alias anchor with no graph links:
two of them are disjoint competing senses by construction. Invented names
throughout; nothing here comes from a real vault.
"""

from __future__ import annotations

from pathlib import Path

#: (folder, title, aliases, one-line summary). Every page is an entity.
ENTITIES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("People", "Ottilie Marsh", ("Ottilie",), "Runs the spring survey rounds for the estuary team."),
    ("People", "Bram Quillfeather", (), "Coordinates the volunteer rota for the estuary team."),
    ("Projects", "Kestrel Hiring Plan", (), "The staffing plan for the coming quarter."),
    ("Projects", "Marlow Quay Survey", (), "The quay survey schedule and its findings."),
)


#: Hub pages (an insight tagged `hub`): a second anchor kind, so a turn that
#: names an entity and a hub is complementary, never a competing pair.
HUBS: tuple[tuple[str, str], ...] = (
    ("Harbor Lantern Budget", "Everything about the lantern restoration budget."),
    ("Tidewater Grant", "Everything about the tidal monitoring grant."),
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _notes(title: str) -> str:
    """Eight distinct sentences, so an anchor has enough material to be budgeted."""
    tag = "".join(word[0] for word in title.split()).lower()
    return "\n## Notes\n\n" + "".join(
        f"- [fact] {title} entry {index}: a distinct detail recorded for the estuary team, "
        f"long enough to cost real budget when it is served with the rest. ^{tag}-{index}\n"
        for index in range(8)
    )


def seed(vault: Path) -> dict[str, str]:
    """Write the entities; return title -> vault-relative path."""
    paths: dict[str, str] = {}
    for folder, title, aliases, summary in ENTITIES:
        entity_type = "person" if folder == "People" else "project"
        alias_line = f"aliases: [{', '.join(aliases)}]\n" if aliases else ""
        relative = f"Knowledge Base/Entities/{folder}/{title}.md"
        _write(
            vault / relative,
            f"---\ntype: entity\nentity_type: {entity_type}\nstatus: active\n{alias_line}"
            f"updated: 2026-09-01\n---\n\n# {title}\n\n## Summary\n\n{summary}\n\n"
            f"{_notes(title)}",
        )
        paths[title] = relative
    for title, summary in HUBS:
        relative = f"Knowledge Base/Notes/Insights/{title.lower().replace(' ', '-')}-hub.md"
        _write(
            vault / relative,
            f"---\ntype: insight\nstatus: active\ntags: [hub]\nupdated: 2026-09-01\n---\n\n"
            f"# {title}\n\n## Summary\n\n{summary}\n\n## Open questions\n\nWhat is left to decide?\n{_notes(title)}",
        )
        paths[title] = relative
    return paths
