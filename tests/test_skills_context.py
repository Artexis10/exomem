"""Vault skills served as session context: what each caller is told, when, and what it may see."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from conftest import initialize_vault_state_offline
from test_context_catalogue import _built, _seed, _write
from test_governance_egress import _external, _reset_caches
from test_working_set_withheld_twins import _withhold

from exomem import (
    activation_conventions,
    commands,
    context_catalogue,
    find_corpus,
    lexstore,
    skills_context,
    state_migration,
    vault_skills,
    working_set_runtime,
)
from exomem.governance import principal
from exomem.writer_lease import invoke_command

KB = "Knowledge Base"
ZETA = f"{KB}/Skills/zeta/SKILL.md"
ALPHA = f"{KB}/Notes/Insights/alpha/SKILL.md"
MID = f"{KB}/Projects/Harbour Ops/runbooks/mid/SKILL.md"
TURN = "Keep a source"
# A turn that names no skill, yet one skill's trigger text covers.
NO_SKILL_NAMED = "how should the engine be tuned"


def _skill(root: Path, rel: str, name: str, description: str, body: str = "Steps.\n") -> None:
    _write(root, rel, f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}")


def _refresh(root: Path) -> None:
    lexstore.ensure_fresh(root)
    _built(root).close()
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()


@pytest.fixture
def skills_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _seed(tmp_path)
    _skill(tmp_path, ZETA, "zeta", "Use when tuning the zeta engine. Never otherwise.", "Steps for the zeta engine tuning.\n")
    _skill(tmp_path, ALPHA, "alpha", "Use when [[mooring-lines]] are rated. Ask first.")
    _skill(tmp_path, MID, "mid", "Use when planning a passage.")
    _write(tmp_path, f"{KB}/Sources/raw/SKILL.md", "---\nname: raw\ndescription: Captured text.\n---\n\n# raw\n")
    _write(tmp_path, f"{KB}/Skills/bad/SKILL.md", "---\nname: bad\n---\n\n# bad\n")
    initialize_vault_state_offline(tmp_path, source="skills fixture")
    _refresh(tmp_path)
    with principal.library_scope():
        yield tmp_path


def _activate(root: Path, caller=None, **kwargs):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    kwargs.setdefault("turn", TURN)
    kwargs.setdefault("max_chars", 2000)
    with principal.request_scope(caller or principal.owner_principal(surface="library")):
        return invoke_command(command, root, **kwargs)


def _names(block: dict) -> list[str]:
    return [item["name"] for item in block["items"]]


def test_first_turn_lists_the_visible_skills_in_name_order(skills_vault):
    block = _activate(skills_vault)["skills"]

    assert block["state"] == "ready" and block["omitted"] == 0
    assert _names(block) == ["alpha", "mid", "zeta"]
    assert [item["ref"] for item in block["items"]] == [ALPHA, MID, ZETA]
    assert block["items"][0]["summary"] == "Use when are rated."


def test_a_later_turn_with_the_continuity_token_repeats_no_item(skills_vault):
    first = _activate(skills_vault)

    second = _activate(skills_vault, continuity=first["continuity"])

    assert second["skills"] == {
        "state": "unchanged", "generation": first["skills"]["generation"], "items": [], "omitted": 0,
    }


def test_a_token_from_before_skills_receives_the_full_catalogue(skills_vault):
    """A continuity token minted without a skills generation is owed the block, as a new conversation is."""
    first = _activate(skills_vault)
    token = first["continuity"]
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert payload.pop("skills_generation") == first["skills"]["generation"]
    legacy = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    second = _activate(skills_vault, continuity=legacy)

    assert second["skills"]["state"] == "ready" and _names(second["skills"]) == ["alpha", "mid", "zeta"]


def test_an_edited_skill_reaches_the_next_turn_under_a_new_generation(skills_vault):
    first = _activate(skills_vault)
    _skill(skills_vault, MID, "mid", "Use when planning a long passage.")
    _refresh(skills_vault)

    second = _activate(skills_vault, continuity=first["continuity"])

    assert second["skills"]["state"] == "ready"
    assert second["skills"]["generation"] != first["skills"]["generation"]
    assert {item["name"]: item["summary"] for item in second["skills"]["items"]}["mid"] == "Use when planning a long passage."


def test_the_off_switch_removes_the_block(skills_vault, monkeypatch):
    monkeypatch.setenv("EXOMEM_ACTIVATION_SKILLS", "off")

    assert "skills" not in _activate(skills_vault)


def test_a_skills_failure_leaves_the_map_and_turn_context_available(skills_vault, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("fixture skills projection unavailable")

    monkeypatch.setattr(skills_context, "compile_block", unavailable)

    packet = _activate(skills_vault)

    assert packet["abstained"] is False and "map" in packet and "turn_slice" in packet
    assert packet["skills"] == {"state": "unavailable", "generation": None, "items": [], "omitted": None}


def test_the_skills_block_takes_nothing_from_the_turn_allowance(skills_vault, monkeypatch):
    with_skills = _activate(skills_vault)
    monkeypatch.setenv("EXOMEM_ACTIVATION_SKILLS", "off")
    without = _activate(skills_vault)

    assert with_skills["skills"]["state"] == "ready"
    assert with_skills["budget"]["used_chars"] == without["budget"]["used_chars"]
    assert with_skills["map"] == without["map"]


def test_native_skills_leave_the_catalogue_but_a_task_still_projects_them(skills_vault):
    packet = _activate(skills_vault, turn=NO_SKILL_NAMED, native_skills=["zeta", 5, ""])

    assert _names(packet["skills"]) == ["alpha", "mid"]
    assert packet["skills"]["omitted"] == 0
    methods = packet["task_context"]["methods"]["items"]
    assert [(item["kind"], item["name"], item["read_ref"]) for item in methods] == [("skill", "zeta", ZETA)]


def test_a_projected_skill_carries_its_whole_description_or_none_of_it(skills_vault):
    """The method counts against the turn allowance: it fits whole, or the turn reports a budget miss, never a cut text."""
    description = "Use when tuning the zeta engine. " + "Check every gauge before and after. " * 12
    _skill(skills_vault, ZETA, "zeta", description, "Steps for the zeta engine tuning.\n")
    _refresh(skills_vault)

    roomy = _activate(skills_vault, turn=NO_SKILL_NAMED, max_chars=6000)
    tight = _activate(skills_vault, turn=NO_SKILL_NAMED, max_chars=2000)

    assert [item["description"] for item in roomy["task_context"]["methods"]["items"]] == [description.strip()]
    assert roomy["budget"]["used_chars"] <= 6000
    assert tight["task_context"]["methods"]["items"] == []
    assert {"role": "task_context", "reason": "budget"} in tight["task_context"]["missing"]
    assert tight["budget"]["used_chars"] <= 2000
    assert [item["summary"] for item in tight["skills"]["items"] if item["name"] == "zeta"] == ["Use when tuning the zeta engine."]


def test_a_caller_who_cannot_see_a_skill_is_told_nothing_of_it(skills_vault):
    _withhold(skills_vault, MID, ceiling=0)
    _refresh(skills_vault)
    restricted = _activate(skills_vault, caller=_external())["skills"]

    (skills_vault / MID).unlink()
    _refresh(skills_vault)
    two_skill_vault = _activate(skills_vault, caller=_external())["skills"]

    assert _names(restricted) == ["alpha", "zeta"] and restricted["omitted"] == 0
    assert restricted["generation"] == two_skill_vault["generation"]


def test_a_store_enrolled_vault_serves_a_skill_page_that_has_an_identity(skills_vault):
    """A release-gated vault withholds a unit that names its page only by path, so an identified skill must stay served."""
    _write(
        skills_vault, ZETA,
        "---\nname: zeta\ndescription: Use when tuning the zeta engine.\nexomem_id: 0b5f3a52-4c1e-4d8e-9a8f-2f7c1d9e6a10\n---\n\n# zeta\n",
    )
    state_migration.record_collection_store_compatibility(skills_vault, authority_check=lambda: True)
    _refresh(skills_vault)

    block = _activate(skills_vault)["skills"]

    assert block["state"] == "ready"
    zeta = next(item for item in block["items"] if item["name"] == "zeta")
    assert zeta["ref"].startswith("exomem://memory/")


def test_opening_a_skill_lists_the_other_files_of_its_directory_the_caller_may_see(skills_vault):
    for rel in ("references/style.md", "references/deep/more.md", "private.md"):
        _write(skills_vault, f"{KB}/Skills/zeta/{rel}", f"---\ntype: insight\nstatus: active\n---\n\n# {rel}\n")
    _write(skills_vault, f"{KB}/Skills/zeta/scripts/run.py", "print(1)\n")
    _withhold(skills_vault, f"{KB}/Skills/zeta/private.md", ceiling=0)
    _refresh(skills_vault)
    read = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")

    owner = invoke_command(read, skills_vault, path=ZETA)
    with principal.request_scope(_external()):
        restricted = invoke_command(read, skills_vault, path=ZETA)
    ordinary = invoke_command(read, skills_vault, path=f"{KB}/Skills/zeta/references/style.md")

    assert owner["skill_files"] == [
        f"{KB}/Skills/zeta/private.md", f"{KB}/Skills/zeta/references/deep/more.md", f"{KB}/Skills/zeta/references/style.md",
    ]
    assert restricted["skill_files"] == [f"{KB}/Skills/zeta/references/deep/more.md", f"{KB}/Skills/zeta/references/style.md"]
    assert "skill_files_partial" not in restricted
    assert "skill_files" not in ordinary


def test_a_skill_above_a_large_tree_costs_a_bounded_read_and_says_its_list_is_partial(skills_vault, monkeypatch):
    """A skill placed above a big folder must not make every read of it walk that whole folder."""
    for index in range(12):
        _write(skills_vault, f"{KB}/Skills/zeta/references/page-{index:02}.md", f"---\ntype: insight\nstatus: active\n---\n\n# {index}\n")
    _refresh(skills_vault)
    monkeypatch.setattr(vault_skills, "SKILL_FILES_SCAN_MAX", 5)
    monkeypatch.setattr(vault_skills, "SKILL_FILES_MAX", 3)
    walked = []
    real_walk = find_corpus.walk_md

    def counting_walk(root, **kwargs):
        for path in real_walk(root, **kwargs):
            walked.append(path)
            yield path

    monkeypatch.setattr(find_corpus, "walk_md", counting_walk)
    read = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")

    opened = invoke_command(read, skills_vault, path=ZETA)

    assert len(opened["skill_files"]) == 3
    assert opened["skill_files_partial"] is True
    assert len(walked) <= 6


def test_a_catalogue_from_before_skills_rebuilds_and_lists_them(skills_vault):
    catalogue = context_catalogue.ContextCatalogue(skills_vault)
    catalogue.close()
    db = sqlite3.connect(catalogue.path)
    db.execute("DROP INDEX entries_skill")
    db.execute("ALTER TABLE entries DROP COLUMN skill")
    db.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    db.commit()
    db.close()

    rebuilt = _built(skills_vault)
    try:
        listed = {entry.skill for entry in rebuilt.entries_by_id(rebuilt.skill_ids())}
    finally:
        rebuilt.close()

    assert listed == {"alpha", "mid", "zeta"}


def _set_budgets(root: Path, **budgets: int) -> None:
    path = activation_conventions.override_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": 1, "budgets": budgets}), encoding="utf-8")
    activation_conventions.clear_cache()


def test_a_vault_without_an_override_keeps_the_shipped_skill_bounds(skills_vault):
    kept, cut = "Use when " + "planning " * 16 + "a passage.", "Use when " + "planning " * 40 + "a passage."
    _skill(skills_vault, MID, "mid", kept)
    _skill(skills_vault, ALPHA, "alpha", cut)
    _refresh(skills_vault)

    summaries = {item["name"]: item["summary"] for item in _activate(skills_vault)["skills"]["items"]}

    assert len(kept) < 200 < len(cut)
    assert summaries["mid"] == kept
    assert len(summaries["alpha"]) <= 200 and summaries["alpha"].endswith("planning…")


def test_a_vault_sets_the_size_of_its_skills_block(skills_vault):
    whole = _activate(skills_vault)["skills"]
    one_line = skills_context.vault_skills.chars({"items": whole["items"][:1]})
    _set_budgets(skills_vault, skills_max_chars=one_line)

    block = _activate(skills_vault)["skills"]

    assert _names(block) == ["alpha"]
    assert block["omitted"] == 2
    assert block["generation"] != whole["generation"]


def test_a_vault_sets_the_length_of_each_skill_summary(skills_vault):
    _skill(skills_vault, MID, "mid", "Use when " + "planning " * 40 + "a passage.")
    _refresh(skills_vault)
    _set_budgets(skills_vault, skill_summary_max_chars=60)

    summary = {item["name"]: item["summary"] for item in _activate(skills_vault)["skills"]["items"]}["mid"]

    assert len(summary) <= 60 and summary.endswith("planning…")


def test_an_invalid_skill_bound_keeps_the_shipped_one_and_is_reported(skills_vault):
    _set_budgets(skills_vault, skills_max_chars=99999)

    packet = _activate(skills_vault)

    assert _names(packet["skills"]) == ["alpha", "mid", "zeta"]
    findings = packet["generation"]["conventions_findings"]
    assert [(finding["code"], finding["field"]) for finding in findings] == [("invalid_budget", "budgets.skills_max_chars")]
