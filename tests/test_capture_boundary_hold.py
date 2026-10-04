"""`capture-survives-contention` part 1: capture writes hold the mutation
boundary only for their commit, never for whole-vault derived work.

The private-identity inventory is a whole-vault walk ("tens of seconds on a
mature vault"). A write that met it cold used to build it inline while it held
the boundary, which is how an `episode_memory` or `record_update` held the
boundary for 14-50 s and starved every other writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture

from exomem import (
    commands,
    mutation_lock,
    record_formats,
    reserved_paths,
    writer_lease,
)
from exomem import schema as schema_module
from exomem import structured_collections as collections
from exomem.governance.principal import owner_principal, request_scope

EPISODE = "ep-" + "b2" * 16


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


def _grow_vault(vault: Path, pages: int) -> None:
    folder = vault / "Knowledge Base" / "Notes" / "Insights"
    for index in range(pages):
        (folder / f"synthetic-note-{index:05d}.md").write_text(
            "---\ntype: insight\nstatus: active\ncreated: 2026-05-22\n"
            f"updated: 2026-05-22\nsources: []\ntags: [topic-{index % 50}]\n---\n\n"
            f"# Synthetic insight {index}\n\n## Claim\n\nBody for note {index}.\n",
            encoding="utf-8",
        )


def _watch_whole_vault_walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The mutation-boundary state at every whole-vault identity walk."""
    states: list[str] = []
    real = reserved_paths.IdentityCatalogue.from_vault.__func__

    def observed(cls, vault_root):
        states.append(str(mutation_lock.active_mutation_snapshot()["state"]))
        return real(cls, vault_root)

    monkeypatch.setattr(reserved_paths.IdentityCatalogue, "from_vault", classmethod(observed))
    return states


def test_a_cold_identity_walk_never_runs_inside_an_episode_boundary(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grow_vault(vault, 300)
    states = _watch_whole_vault_walks(monkeypatch)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    command = next(c for c in commands.product_commands_for("mcp") if c.name == "episode_memory")

    with request_scope(owner_principal(surface="mcp")):
        result = manager.invoke(
            command,
            (vault, schema_module.load_source_schema(vault)),
            {
                "action": "record",
                "episode": EPISODE,
                "subject": "Harbor Lamp purchase",
                "summary": "Chose the brass lamp; delivery date still open.",
                "decided": ["Buy the brass Harbor Lamp"],
            },
            implicit_idempotency_scope="principal:test",
        )

    assert result["source"]["path"]
    assert states, "the cold inventory should have been built by this write"
    assert set(states) == {"free"}


def test_a_cold_identity_walk_never_runs_inside_a_record_update_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    log = tmp_path / "Knowledge Base/log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    record = next(item for item in parsed.records if item.identity.key.startswith("14d2bdca"))
    reserved_paths._BASELINE_IDENTITY_CATALOGUES.clear()
    states = _watch_whole_vault_walks(monkeypatch)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    command = next(c for c in commands.product_commands_for("mcp") if c.name == "record_memory")

    result = manager.invoke(
        command,
        (tmp_path,),
        {
            "action": "update",
            "collection": (fixture / "_collection.md").relative_to(tmp_path).as_posix(),
            "item_key": record.identity.key,
            "changes": {"status": "completed"},
            "expected_container_hash": parsed.snapshot,
            "expected_item_version": record.source.hash,
            "why": "correct the maintenance status",
        },
        implicit_idempotency_scope="principal:test",
    )

    assert result
    assert states, "the cold inventory should have been built by this write"
    assert set(states) == {"free"}


def _oracle_notes_by_subfolder(notes_dir: Path) -> dict[str, dict[str, int]]:
    """The pre-scandir `rglob` counting rules, kept as the equivalence oracle."""
    out: dict[str, dict[str, int]] = {}
    for type_folder in notes_dir.iterdir():
        if not type_folder.is_dir() or type_folder.name.startswith("_"):
            continue
        inner: dict[str, int] = {}
        subs = [c for c in type_folder.iterdir() if c.is_dir() and not c.name.startswith("_")]
        if subs:
            for sub in subs:
                inner[sub.name] = sum(1 for p in sub.rglob("*.md") if p.name != "index.md")
            top = sum(1 for p in type_folder.glob("*.md") if p.name != "index.md")
            if top:
                inner[""] = top
        else:
            inner[""] = sum(1 for p in type_folder.rglob("*.md") if p.name != "index.md")
        out[type_folder.name] = inner
    return out


def test_index_counts_match_the_rglob_rules_they_replaced(tmp_path: Path) -> None:
    from exomem import indexes

    notes = tmp_path / "Notes"
    for rel in (
        "Research/Project Alpha/a.md",
        "Research/Project Alpha/deep/b.md",
        "Research/Project Alpha/deep/index.md",
        "Research/Project Alpha/_hidden/c.md",
        "Research/Project Alpha/notes.txt",
        "Research/loose.md",
        "Insights/one.md",
        "Insights/two.md",
        "Insights/index.md",
        "_Scratch/ignored.md",
    ):
        path = notes / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    assert indexes._count_notes_by_subfolder(notes) == _oracle_notes_by_subfolder(notes)
    assert indexes._count_notes(notes) == {"research": 4, "insight": 2}

    sources = tmp_path / "Sources"
    for rel in ("Articles/a.md", "Articles/2026/b.md", "Articles/_x/c.md", "Books/d.md"):
        path = sources / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    assert indexes._count_sources(sources) == {"Articles": 2, "Books": 1}


def test_a_directory_named_like_a_page_is_counted_and_descended_as_rglob_did(
    tmp_path: Path,
) -> None:
    from exomem import indexes

    notes = tmp_path / "Notes"
    (notes / "Research" / "Alpha" / "dir.md").mkdir(parents=True)
    (notes / "Research" / "Alpha" / "dir.md" / "inner.md").write_text("x", encoding="utf-8")
    (notes / "Research" / "Alpha" / "a.md").write_text("x", encoding="utf-8")
    sources = tmp_path / "Sources"
    (sources / "Articles" / "dir.md").mkdir(parents=True)
    (sources / "Articles" / "dir.md" / "inner.md").write_text("x", encoding="utf-8")

    assert indexes._count_notes_by_subfolder(notes) == _oracle_notes_by_subfolder(notes)
    assert indexes._count_notes_by_subfolder(notes) == {"Research": {"Alpha": 3}}
    assert indexes._count_sources(sources) == {"Articles": 2}
