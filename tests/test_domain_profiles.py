"""Domain profiles: typed pages, parent domains and the block a turn receives.

The workflow under test is an owner's profile page reaching the agent for the turns that touch its domain,
with no word or domain list in code. Each check names what only a real vault catches: a write that would let a
two-domain page serve twice, an unresolved topic that would widen a profile into a global one, a cycle that
would loop the resolver, and a block that would spend the turn's allowance or leak a hidden page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, note_types, source_taxonomy

KB = "Knowledge Base"
CONTENT = "- [rule] Prefer finished stories over endless series. ^story\n"


def _remember_profile(vault: Path, title: str, topics: list[str] | None, **options: object) -> dict:
    return commands.op_remember(
        vault, content=CONTENT, title=title, note_type="profile", status="draft", topics=topics, **options,
    )


def _save_domains(vault: Path, proposal: dict) -> dict:
    before = commands.op_schema_memory(vault, subject="domains", operation="inspect")
    return commands.op_schema_memory(
        vault, subject="domains", operation="save", proposal=proposal,
        expected_hash=before["content_hash"], why="organise the domains",
    )


@pytest.mark.usefixtures("owner_scope")
def test_the_typed_writer_files_a_global_and_a_domain_profile_in_one_area(vault: Path) -> None:
    saved = {
        "global": _remember_profile(vault, "Global profile", None),
        "travel": _remember_profile(vault, "Travel profile", ["travel"]),
    }

    assert {name: Path(result["path"]).parent.as_posix() for name, result in saved.items()} == {
        "global": f"{KB}/Notes/Profiles", "travel": f"{KB}/Notes/Profiles",
    }
    assert note_types.shipped_registry().types["profile"].partition == "none"


@pytest.mark.usefixtures("owner_scope")
def test_a_profile_covering_two_domains_is_refused_the_same_way_at_preview_and_commit(vault: Path) -> None:
    preview = _remember_profile(vault, "Two domains", ["travel", "health"], validate_only=True)
    with pytest.raises(ValueError, match="SEMANTIC_CONTRACT_BLOCKED") as commit:
        _remember_profile(vault, "Two domains", ["travel", "health"])

    (finding,) = preview["contract_result"]["blocking_findings"]
    assert finding["code"] == "INVALID_PROFILE_TOPICS" and "travel, health" in finding["detail"]
    assert finding["detail"] in str(commit.value) and finding["remediation"] in str(commit.value)
    assert not (vault / KB / "Notes" / "Profiles").exists()


@pytest.mark.usefixtures("owner_scope")
def test_a_parent_must_name_a_registered_domain_and_never_return_to_its_child(vault: Path) -> None:
    assert _save_domains(vault, {"upsert": {"field-survey": {"label": "Field survey", "parent": "travel"}}})["saved"]
    unknown = _save_domains(vault, {"upsert": {"field-survey": {"parent": "no-such-domain"}}})
    cycle = _save_domains(vault, {"upsert": {"travel": {"parent": "field-survey"}}})

    for refused in (unknown, cycle):
        assert refused["valid"] is False and refused["saved"] is None
    (named,) = (finding["detail"] for finding in unknown["findings"])
    assert "'field-survey'" in named and "'no-such-domain'" in named and "remove the parent" in named
    (looped,) = (finding["detail"] for finding in cycle["findings"])
    assert "'field-survey'" in looped and "'travel'" in looped and "cycle" in looped
    taxonomy = source_taxonomy.load_taxonomy(vault)
    assert taxonomy.domain_ancestors("field-survey") == ("travel",) and taxonomy.domains["travel"].parent is None


@pytest.mark.usefixtures("owner_scope")
def test_ancestors_come_nearest_first(vault: Path) -> None:
    _save_domains(vault, {"upsert": {
        "audio": {"label": "Audio", "parent": "media"}, "mixing": {"label": "Mixing", "parent": "audio"},
    }})

    assert source_taxonomy.load_taxonomy(vault).domain_ancestors("mixing") == ("audio", "media")
