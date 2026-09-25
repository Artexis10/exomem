"""`learned_aliases`: a name the owner's agent learned from a correction
(close-memory-loop step 5, lane B, task B3).

A learned name lives on the anchor's own page, in its own frontmatter list,
and is read into the anchor's activation aliases only: never into the page
names that resolve a wikilink or that the egress guard matches, so it changes
what a turn activates and nothing else. The index validates each entry the
way it validates a derived short name, and skips (and counts) one that could
match every turn containing a function word.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_working_set_index import _seed_planning, _seed_structure

from exomem import commands, lexstore, working_set_index, working_set_runtime, writer_lease
from exomem.vault import content_hash

SLED = "Knowledge Base/Products/Cargo Sled.md"
LEDGER = "Knowledge Base/Systems/Depot Ledger.md"


@pytest.fixture
def anchor_vault(vault: Path) -> Path:
    _seed_structure(vault)
    _seed_planning(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _learn(vault: Path, rel: str, names: list[str]) -> dict:
    """The advisory's `name` option, exactly as an agent carries it out."""
    text = (vault / rel).read_text(encoding="utf-8")
    return writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=rel,
        why="the user calls it this",
        operation={
            "kind": "patch_frontmatter",
            "field": "learned_aliases",
            "value": names,
            "expected_hash": content_hash(text),
        },
    )


def _update(vault: Path) -> working_set_index.WorkingSetIndex:
    index = working_set_index.WorkingSetIndex(vault)
    index.update()
    working_set_runtime.reset_caches_for_tests()
    return index


def _resolved(packet: dict) -> list[str]:
    return [item["path"] for item in packet["anchors"] if item["status"] == "resolved"]


def _aliases(index: working_set_index.WorkingSetIndex, path: str) -> tuple[str, ...]:
    return next(row.aliases for row in index.anchors() if row.path == path)


def test_a_learned_alias_resolves_its_anchor_after_the_index_update(anchor_vault: Path) -> None:
    turn = "wie geht es dem Schlitten"
    assert _resolved(commands.op_activate_context(anchor_vault, turn=turn)) == []

    _learn(anchor_vault, SLED, ["Schlitten"])
    _update(anchor_vault)

    packet = commands.op_activate_context(anchor_vault, turn=turn)
    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])
    assert "exact_alias" in packet["anchors"][0]["evidence"]


def test_a_learned_alias_changes_no_wikilink_or_egress_name(anchor_vault: Path) -> None:
    kb = anchor_vault / "Knowledge Base"
    (kb / "Notes" / "Insights" / "winter-towing.md").write_text(
        "---\ntype: insight\nstatus: active\ntags: [hub]\n---\n\n# Winter towing\n\n"
        "Check the [[Schlitten]] before every run.\n",
        encoding="utf-8",
    )
    _learn(anchor_vault, SLED, ["Schlitten"])
    index = _update(anchor_vault)

    assert "schlitten" in _aliases(index, SLED)
    conn = sqlite3.connect(index.path)
    try:
        names = {name for (name,) in conn.execute("SELECT name FROM page_names")}
        links = set(
            conn.execute(
                "SELECT anchor_id, other_path FROM anchor_links "
                "WHERE anchor_id LIKE '%winter-towing.md'"
            )
        )
    finally:
        conn.close()
    # The page names the wikilink resolver and the egress guard match stay
    # the owner's: the learned name is not one of them, so the link dangles.
    assert "schlitten" not in names
    assert "cargo sled" in names
    assert all(other != SLED for _anchor, other in links)


def test_a_learned_alias_of_function_words_or_a_short_word_is_skipped(anchor_vault: Path) -> None:
    _learn(
        anchor_vault,
        SLED,
        ["the thing", "it", "ok so", "ab", "x" * 65, "Schlitten", "Zugschlitten"],
    )
    index = _update(anchor_vault)

    aliases = _aliases(index, SLED)
    assert "schlitten" in aliases and "zugschlitten" in aliases
    for rejected in ("the thing", "it", "ok so", "ab", "x" * 65):
        assert rejected not in aliases
    packet = commands.op_activate_context(anchor_vault, turn="is it ok so far")
    assert SLED not in _resolved(packet)
    assert packet["generation"]["learned_aliases_rejected"] == 5


def test_at_most_eight_learned_aliases_are_read(anchor_vault: Path) -> None:
    names = [f"schlittenname{index}" for index in range(10)]
    _learn(anchor_vault, SLED, names)
    index = _update(anchor_vault)

    aliases = _aliases(index, SLED)
    assert sum(alias.startswith("schlittenname") for alias in aliases) == 8
    packet = commands.op_activate_context(anchor_vault, turn="the schlittenname9 please")
    assert packet["generation"]["learned_aliases_rejected"] == 2


def test_a_packet_reports_no_rejections_when_there_are_none(anchor_vault: Path) -> None:
    packet = commands.op_activate_context(anchor_vault, turn="tell me about the Cargo Sled")
    assert "learned_aliases_rejected" not in packet["generation"]


def test_edit_memory_warns_about_a_learned_alias_the_index_will_skip(anchor_vault: Path) -> None:
    result = _learn(anchor_vault, SLED, ["it", "Schlitten"])

    warnings = " ".join(str(item) for item in result.get("warnings") or ())
    assert "learned_aliases" in warnings
    assert "'it'" in warnings
    assert "Schlitten" not in warnings


def test_a_learned_alias_shared_by_two_anchors_is_ambiguous(anchor_vault: Path) -> None:
    """Never one of them. Two unlinked anchors of one kind sharing a learned
    name are competing senses; the agent picks."""
    plough = "Knowledge Base/Products/Snow Plough.md"
    (anchor_vault / plough).write_text(
        "---\ntype: note\nstatus: active\n---\n\n# Snow Plough\n\n## Summary\n\n"
        "A blade for the depot yard.\n",
        encoding="utf-8",
    )
    _update(anchor_vault)
    _learn(anchor_vault, SLED, ["Vorratsding"])
    _learn(anchor_vault, plough, ["Vorratsding"])
    _update(anchor_vault)

    packet = commands.op_activate_context(anchor_vault, turn="was ist mit dem Vorratsding")
    assert packet["abstained"] is True
    assert packet["abstention"]["reason"] == "ambiguous"
    assert {item["title"] for item in packet["ambiguity"]} == {"Cargo Sled", "Snow Plough"}


def test_a_learned_alias_shared_by_two_linked_anchors_serves_both(anchor_vault: Path) -> None:
    """The Depot Ledger links to the Cargo Sled, so the two are complementary
    under the resolver's ordinary rule, exactly as a shared owner alias
    would be: both are served, never one of them."""
    _learn(anchor_vault, SLED, ["Vorratsding"])
    _learn(anchor_vault, LEDGER, ["Vorratsding"])
    _update(anchor_vault)

    packet = commands.op_activate_context(anchor_vault, turn="was ist mit dem Vorratsding")
    assert sorted(_resolved(packet)) == sorted([SLED, LEDGER])


@pytest.mark.parametrize(
    ("name", "turn"),
    [("Сани", "как там сани сегодня"), ("雪ぞり", "雪ぞり")],
    ids=["cyrillic", "japanese"],
)
def test_a_learned_alias_in_a_non_latin_script_resolves(
    anchor_vault: Path, name: str, turn: str
) -> None:
    """NFKC and casefold, the same fold a turn gets. A script without word
    separators is one token, so a Japanese name matches a turn that is that
    token (the residual segmentation limit, recorded in the design)."""
    _learn(anchor_vault, SLED, [name])
    _update(anchor_vault)

    packet = commands.op_activate_context(anchor_vault, turn=turn)
    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])
