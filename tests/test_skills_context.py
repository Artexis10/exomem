"""Vault skills and Records collections served as session context: what each caller is told, when, and what it may see."""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
import yaml
from conftest import initialize_vault_state_offline
from test_context_catalogue import _built, _seed, _write
from test_domain_profiles import GAMES_TURN, GENERAL_TURN, _seed_profiles
from test_domain_profiles import _activate as _activate_profiles
from test_domain_profiles import _set_budgets as _set_profile_budgets
from test_governance_egress import _external, _reset_caches
from test_working_set_index import _seed_planning
from test_working_set_withheld_twins import _withhold

from exomem import (
    activation_conventions,
    commands,
    context_catalogue,
    context_views,
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
    # The pointer at the catalogue is turn protocol prose, charged like any `next_action`; the block itself is not.
    def turn_chars(packet):
        return packet["budget"]["used_chars"] - len((packet.get("next_action") or {}).get("say", ""))

    assert turn_chars(with_skills) == turn_chars(without)
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
    # Half of 1,000 is the turn's share, too small for this description.
    tight = _activate(skills_vault, turn=NO_SKILL_NAMED, max_chars=1000)

    assert [item["description"] for item in roomy["task_context"]["methods"]["items"]] == [description.strip()]
    assert roomy["budget"]["used_chars"] <= 6000
    assert tight["task_context"]["methods"]["items"] == []
    assert {"role": "task_context", "reason": "budget"} in tight["task_context"]["missing"]
    assert tight["budget"]["used_chars"] <= 1000
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


# --------------------------------------------------------------------------- #
# Records collections beside the skills
# --------------------------------------------------------------------------- #

DEPOT_BODY = "Observations of depot stock. Never edit a past row.\n\n## States\n\nA closed row needs evidence."


def _collection(root: Path, folder: str, title: str, body: str, topics: tuple[str, ...] = ()) -> str:
    """A Records collection manifest at `Records/<folder>`; `topics` is the field a domain registry resolves."""
    declared = f"topics: [{', '.join(topics)}]\n" if topics else ""
    (root / KB / "Records" / folder / "Items").mkdir(parents=True, exist_ok=True)
    return _write(
        root, f"{KB}/Records/{folder}/_collection.md",
        f"---\ntype: collection\nexomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, title)}\ntitle: {title}\n"
        "semantic_profile: records\ncollection_version: 1\nschema_version: 1\nlifecycle: active\n"
        "storage:\n  strategy: markdown-items\n  source: Items\n  format_version: 1\n"
        "item_schema:\n  natural_key: [observed_on]\n  fields:\n    observed_on: {type: date, required: true}\n"
        f"{declared}---\n\n{body}\n",
    )


def _read(root: Path, ref: str) -> dict:
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")
    with principal.request_scope(principal.owner_principal(surface="library")):
        return invoke_command(command, root, path=ref)


def test_a_records_collection_is_listed_after_the_skills_with_its_kind_name_summary_and_ref(skills_vault):
    manifest = _collection(skills_vault, "Depot Stock", "Depot stock", DEPOT_BODY)
    _refresh(skills_vault)

    block = _activate(skills_vault)["skills"]

    assert [(item["kind"], item["name"]) for item in block["items"]] == [
        ("skill", "alpha"), ("skill", "mid"), ("skill", "zeta"), ("collection", "Depot stock"),
    ]
    collection = block["items"][-1]
    assert collection["summary"] == "Observations of depot stock."
    opened = _read(skills_vault, collection["ref"])
    assert opened["path"] == manifest and "A closed row needs evidence." in opened["body"]


def test_a_planning_collection_is_not_listed_because_only_records_collections_are(skills_vault):
    _collection(skills_vault, "Depot Stock", "Depot stock", DEPOT_BODY)
    _write(skills_vault, f"{KB}/log.md", "# Log\n")  # Planning writes need the activity log.
    _seed_planning(skills_vault)
    _refresh(skills_vault)

    names = _names(_activate(skills_vault)["skills"])

    assert "Depot stock" in names and len(names) == 4


def test_a_collection_the_caller_cannot_see_changes_no_item_count_or_generation(skills_vault):
    hidden = _collection(skills_vault, "Depot Stock", "Depot stock", DEPOT_BODY)
    _withhold(skills_vault, hidden, ceiling=0)
    _refresh(skills_vault)
    restricted = _activate(skills_vault, caller=_external())["skills"]

    (skills_vault / hidden).unlink()
    _refresh(skills_vault)
    without_collection = _activate(skills_vault, caller=_external())["skills"]

    assert _names(restricted) == ["alpha", "mid", "zeta"] and restricted["omitted"] == 0
    assert restricted["generation"] == without_collection["generation"]


def test_native_skills_omit_only_skills_so_a_collection_with_a_native_name_stays(skills_vault):
    _collection(skills_vault, "Depot Stock", "zeta", DEPOT_BODY)
    _refresh(skills_vault)

    block = _activate(skills_vault, native_skills=["zeta"])["skills"]

    assert [(item["kind"], item["name"]) for item in block["items"]] == [
        ("skill", "alpha"), ("skill", "mid"), ("collection", "zeta"),
    ]


def test_advice_tells_the_agent_to_open_a_covering_item_and_is_sent_only_with_items(skills_vault):
    first = _activate(skills_vault)
    later = _activate(skills_vault, continuity=first["continuity"])
    for skill in (ZETA, ALPHA, MID):
        (skills_vault / skill).unlink()
    _refresh(skills_vault)
    emptied = _activate(skills_vault)

    advice = first["skills"]["advice"]
    assert "skills and collections" in advice and "read_memory" in advice and "existing authority" in advice
    assert "advice" not in later["skills"] and later["skills"]["state"] == "unchanged"
    assert emptied["skills"]["state"] == "empty" and "advice" not in emptied["skills"]


def test_the_turn_that_receives_the_catalogue_is_pointed_at_it_first(skills_vault):
    """A tool-only client follows `next_action`: on the catalogue's turn it checks the catalogue, not retrieval leads."""
    first = _activate(skills_vault)
    later = _activate(skills_vault, continuity=first["continuity"])

    assert first["skills"]["state"] == "ready"
    assert first["next_action"]["state"] == "skill_check" and first["next_action"]["tool"] == "read_memory"
    assert later["skills"]["state"] == "unchanged"
    assert (later.get("next_action") or {}).get("state") != "skill_check"


# --------------------------------------------------------------------------- #
# A catalogue that overflows its bound keeps its listing and names the turn's items beside it
# --------------------------------------------------------------------------- #


def _one_item_bound(root: Path, items: list[dict]) -> None:
    """Set the vault's bound to its longest line, so exactly one item is listed and `omitted` counts the others.

    The profile budgets stay, or the profile floor would fall back to the shipped one and touch no domain.
    """
    _set_profile_budgets(root, skills_max_chars=max(vault_skills.chars({"items": [item]}) for item in items))


def _topic_skill(root: Path, name: str, *topics: str) -> str:
    rel = f"{KB}/Skills/{name}/SKILL.md"
    declared = f"topics: [{', '.join(topics)}]\n" if topics else ""
    _write(root, rel, f"---\nname: {name}\ndescription: Use when {name} applies.\n{declared}---\n\n# {name}\n\nSteps.\n")
    return rel


@pytest.fixture
def domain_vault(tmp_path, monkeypatch):
    """A registry with media > games, music and cooking, a profile page for each, and no skill or collection yet."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _seed_profiles(tmp_path)
    _set_profile_budgets(tmp_path)
    initialize_vault_state_offline(tmp_path, source="catalogue overflow fixture")
    with principal.library_scope():
        yield tmp_path


def _turn(block: dict) -> list[str]:
    return [item["name"] for item in block.get("turn_items") or ()]


def _listed(root: Path, turn: str, items: list[tuple[str, str]], touched: list[str]) -> dict:
    """The catalogue for `turn` when only one of `items` (kind, name) fits.

    Asserts that the whole catalogue held every item first, and that the turn touched exactly `touched` (the domain
    profiles served, nearest first), so the order under test comes from the reading and not from a stray match.
    """
    _refresh(root)
    whole = _activate_profiles(root, turn)["skills"]
    assert sorted((item["kind"], item["name"]) for item in whole["items"]) == sorted(items) and whole["omitted"] == 0
    _one_item_bound(root, whole["items"])
    packet = _activate_profiles(root, turn)
    assert [entry["domain"] for entry in packet["domain_profiles"]["profiles"]] == touched
    return packet["skills"]


# The names share no word with the turns, so a lexical match of the turn never reaches a skill page on its own.
def test_an_overflowing_catalogue_names_the_unlisted_item_of_the_touched_domain_for_the_turn(domain_vault):
    _topic_skill(domain_vault, "alpha-pantry", "cooking")
    _topic_skill(domain_vault, "zulu-lantern", "games")

    block = _listed(domain_vault, GAMES_TURN, [("skill", "alpha-pantry"), ("skill", "zulu-lantern")], ["games", "media"])

    assert _names(block) == ["alpha-pantry"] and block["omitted"] == 1
    assert _turn(block) == ["zulu-lantern"]


def test_an_overflowing_catalogue_names_an_unlisted_collection_of_the_touched_domain(domain_vault):
    _topic_skill(domain_vault, "alpha-pantry", "cooking")
    _collection(domain_vault, "Game Nights", "Zulu evenings", "Tabletop evenings and who won.", topics=("games",))

    block = _listed(domain_vault, GAMES_TURN, [("skill", "alpha-pantry"), ("collection", "Zulu evenings")], ["games", "media"])

    assert _names(block) == ["alpha-pantry"] and block["omitted"] == 1
    assert _turn(block) == ["Zulu evenings"]


def test_an_overflowing_catalogue_names_an_item_of_the_ancestor_domain_of_the_touched_one(domain_vault):
    _topic_skill(domain_vault, "alpha-pantry", "cooking")
    _topic_skill(domain_vault, "zulu-shelves", "media")

    block = _listed(domain_vault, GAMES_TURN, [("skill", "alpha-pantry"), ("skill", "zulu-shelves")], ["games", "media"])

    assert _names(block) == ["alpha-pantry"] and block["omitted"] == 1
    assert _turn(block) == ["zulu-shelves"]


def test_an_overflowing_catalogue_with_no_touched_domain_names_no_turn_items(domain_vault):
    _topic_skill(domain_vault, "alpha-pantry", "cooking")
    _topic_skill(domain_vault, "zulu-lantern", "games")

    block = _listed(domain_vault, GENERAL_TURN, [("skill", "alpha-pantry"), ("skill", "zulu-lantern")], [])

    assert _names(block) == ["alpha-pantry"] and block["omitted"] == 1
    assert "turn_items" not in block


def test_a_catalogue_that_fits_ignores_the_turn_and_stays_unchanged_across_turns(domain_vault):
    _topic_skill(domain_vault, "alpha-pantry", "cooking")
    _topic_skill(domain_vault, "zulu-lantern", "games")
    _refresh(domain_vault)

    games = _activate_profiles(domain_vault, GAMES_TURN)
    general = _activate_profiles(domain_vault, GENERAL_TURN)
    next_turn = _activate_profiles(domain_vault, GENERAL_TURN, continuity=games["continuity"])

    assert _names(games["skills"]) == _names(general["skills"]) == ["alpha-pantry", "zulu-lantern"]
    assert games["skills"]["generation"] == general["skills"]["generation"]
    assert next_turn["skills"]["state"] == "unchanged"


def test_an_overflowing_catalogue_names_the_page_the_task_context_reached_when_no_domain_is_touched(skills_vault):
    whole = _activate(skills_vault, turn=NO_SKILL_NAMED)["skills"]
    _one_item_bound(skills_vault, whole["items"])

    block = _activate(skills_vault, turn=NO_SKILL_NAMED)["skills"]

    assert _names(whole) == ["alpha", "mid", "zeta"]
    assert _names(block) == ["alpha"] and block["omitted"] == 2
    assert _turn(block) == ["zeta"]


def test_an_item_of_the_touched_domain_outranks_an_item_the_task_context_reached(skills_vault):
    _write(skills_vault, ZETA, "---\nname: zeta\ndescription: Use when tuning.\ntopics: [fleet]\n---\n\n# zeta\n")
    _refresh(skills_vault)
    whole = _activate(skills_vault)["skills"]
    _one_item_bound(skills_vault, whole["items"])
    caller = context_views.CallerView.current(skills_vault)

    block = skills_context.compile_block(skills_vault, caller, domains=["fleet"], reached=[MID])

    assert _names(block) == ["alpha"] and block["omitted"] == 2
    assert _turn(block) == ["zeta", "mid"]


def test_an_overflowing_catalogue_is_held_once_while_the_turns_domains_change(domain_vault):
    """The listing reads no turn, so a held catalogue stays unchanged and is not pointed at again; the turn's items still move."""
    _topic_skill(domain_vault, "alpha-pantry", "cooking")
    _topic_skill(domain_vault, "zulu-lantern", "games")
    _listed(domain_vault, GAMES_TURN, [("skill", "alpha-pantry"), ("skill", "zulu-lantern")], ["games", "media"])

    first = _activate_profiles(domain_vault, GAMES_TURN)
    second = _activate_profiles(domain_vault, GENERAL_TURN, continuity=first["continuity"])
    third = _activate_profiles(domain_vault, GAMES_TURN, continuity=second["continuity"])

    assert first["skills"]["state"] == "ready" and first["next_action"]["state"] == "skill_check"
    for later in (second, third):
        assert later["skills"]["state"] == "unchanged" and later["skills"]["items"] == []
        assert (later.get("next_action") or {}).get("state") != "skill_check"
    assert _turn(second["skills"]) == [] and _turn(third["skills"]) == ["zulu-lantern"]
