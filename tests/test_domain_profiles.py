"""Domain profiles: typed pages, parent domains and the block a turn receives.

The workflow under test is an owner's profile page reaching the agent for the turns that touch its domain,
with no word or domain list in code. Each check names what only a real vault catches: a write that would let a
two-domain page serve twice, an unresolved topic that would widen a profile into a global one, a cycle that
would loop the resolver, and a block that would spend the turn's allowance or leak a hidden page.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import initialize_vault_state_offline
from test_activation_related_pages import AXIS, FINGERPRINT, _passages, _planted_vault, _random_unit, _toward_turn
from test_context_catalogue import _built, _page, _write
from test_governance_egress import _external, _reset_caches
from test_working_set_withheld_twins import _withhold

from exomem import (
    activation_conventions,
    commands,
    context_catalogue,
    context_views,
    domain_profiles,
    embeddings,
    epistemic_graph,
    lexstore,
    note_types,
    profile_selection,
    ranking_config,
    readiness,
    recall_policy,
    recall_space,
    source_taxonomy,
    stance,
    working_set_index,
    working_set_runtime,
)
from exomem._hooks import exomem_retrieve_nudge as hook
from exomem.governance import principal
from exomem.writer_lease import invoke_command

KB = "Knowledge Base"
PROFILES = f"{KB}/Notes/Profiles"
CONTENT = "- [rule] Prefer finished stories over endless series. ^story\n"


def _remember_profile(vault: Path, title: str, topics: list[str] | None, **options: object) -> dict:
    """Preview the profile and, unless the caller wants only the preview, commit with that preview's hash."""
    page = {"content": CONTENT, "title": title, "note_type": "profile", "status": "draft", "topics": topics}
    preview = commands.op_remember(vault, **page, **{**options, "validate_only": True})
    if options.get("validate_only"):
        return preview
    reviewed = {"draft_id": preview["draft_id"], "draft_hash": preview["draft_hash"], "draft_token": preview["draft_token"]}
    return commands.op_remember(vault, **page, **options, **reviewed)


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


@pytest.mark.usefixtures("owner_scope")
def test_an_unregistered_topic_is_reported_without_blocking_the_write(vault: Path) -> None:
    preview = _remember_profile(vault, "Silent cinema", ["silent-cinema"], validate_only=True)

    (finding,) = preview["contract_result"]["warnings"]
    assert finding["code"] == "UNRESOLVED_PROFILE_TOPIC" and "silent-cinema" in finding["detail"]
    assert not preview["contract_result"]["blocking_findings"]


# --------------------------------------------------------------------------- #
# What a turn receives
# --------------------------------------------------------------------------- #

TAXONOMY = (
    "schema_version: 1\n"
    "domains:\n"
    "  media: {label: Media}\n"
    "  games: {label: Games, parent: media}\n"
    "  music: {label: Music, parent: media}\n"
    "  cooking: {label: Cooking}\n"
)
# Each profile speaks its own words, so a turn about one domain retrieves that domain's page alone.
RULE = {
    "global": "Keep a source for each claim.",
    "media": "Finished stories beat endless series.",
    "games": "Tactical cooperative play only, never twitch shooters.",
    "music": "Analogue warm albums only, never loudness masters.",
    "cooking": "Slow braises only, never deep frying.",
}
GAMES_TURN = "Which tactical cooperative games suit tonight"
BOTH_TURN = "Pick analogue warm albums and tactical cooperative games"
THREE_TURN = "Pick analogue warm albums, tactical cooperative games and slow braises"
GENERAL_TURN = "What is the boiling point of water at altitude"
# Names no word that any profile page holds, so only a reading of its meaning can reach the games profile.
PARAPHRASE_TURN = "Something fun for the table tonight with friends"
# Five short pages score a matching word far below the shipped keyword floor, so a test vault sets its own.
SMALL_VAULT_LIMITS = {"profile_lexical_floor": 1.0}


def _profile(root: Path, name: str, topics: list[str] | None, text: str, extra: str = "") -> str:
    declared = "" if topics is None else f"topics: [{', '.join(topics)}]\n"
    return _write(
        root, f"{PROFILES}/{name}.md",
        f"---\ntype: profile\nstatus: active\n{declared}{extra}---\n\n# {name}\n\n{text}",
    )


def _rule(name: str, text: str | None = None) -> str:
    return f"- [rule] {text or RULE[name]} ^{name}\n"


def _refresh(root: Path) -> None:
    _reset_caches()
    lexstore.ensure_fresh(root)
    _built(root).close()
    working_set_runtime.reset_caches_for_tests()


def _set_budgets(root: Path, **budgets: float) -> None:
    path = activation_conventions.override_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": 1, "budgets": {**SMALL_VAULT_LIMITS, **budgets}}), encoding="utf-8")
    activation_conventions.clear_cache()


def _seed_profiles(root: Path) -> None:
    """The domain registry, the schema the profile type needs, one global profile and one per domain."""
    _write(root, f"{KB}/_Schema/source-taxonomy.yaml", TAXONOMY)
    _write(root, f"{KB}/_Schema/SKILL.md", "# Memory schema\n")
    schema = Path(stance.__file__).parent / "_scaffold/_Schema/references"
    for name in ("page-types.md",):
        _write(root, f"{KB}/_Schema/references/{name}", (schema / name).read_text(encoding="utf-8"))
    _profile(root, "global", None, _rule("global"))
    for domain in ("media", "games", "music", "cooking"):
        _profile(root, domain, [domain], _rule(domain))


@pytest.fixture
def profiles_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _seed_profiles(tmp_path)
    _set_budgets(tmp_path)
    initialize_vault_state_offline(tmp_path, source="domain profiles fixture")
    _refresh(tmp_path)
    with principal.library_scope():
        yield tmp_path


def _encoder_present(monkeypatch) -> None:
    """A resident activation encoder that maps every turn to the same unit vector, `AXIS`; passages are random."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_activation_passages", _passages)
    monkeypatch.setattr(embeddings, "embed_activation_passages_if_loaded", _passages)
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: AXIS)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(recall_space, "resident_fingerprint", lambda model: FINGERPRINT)


def _plant_profile_chunks(root: Path) -> int:
    """One recall chunk per profile page, the games page's near the turn vector, and every index built.

    Returns the number of anchors that carry a vector, which decides whether the turn slice encodes the turn.
    """
    _reset_caches()
    recall = embeddings.get_embedding_index(root)
    for domain in ("global", "media", "games", "music", "cooking"):
        rel = f"{PROFILES}/{domain}.md"
        chunk = _toward_turn(0.8) if domain == "games" else _random_unit(rel)
        recall.upsert_file(rel, ["profile chunk"], chunk[None, :], 1.0)
    recall.all_vectors()
    lexstore.ensure_fresh(root)
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    anchors = len(index.vector_matrix(FINGERPRINT)[0])
    index.close()
    _built(root).close()
    working_set_runtime.reset_caches_for_tests()
    return anchors


@pytest.fixture
def semantic_vault(vault, monkeypatch):
    """Fifty background anchors, enough for the turn slice to encode, and a games page chunk near the turn's vector.

    The turn vector is planted (a real encoder is out of the loop); the vault, the catalogue, the recall matrix,
    the lexical index, the anchor lane and egress are real. The vault keeps the shipped selection limits.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _planted_vault(vault, monkeypatch, {})
    _seed_profiles(vault)
    assert _plant_profile_chunks(vault) >= ranking_config.DEFAULT_RANKING.working_set_semantic_min_population
    with principal.library_scope():
        yield vault


@pytest.fixture
def small_semantic_vault(tmp_path, monkeypatch):
    """The same planted encoder over a catalogue under the slice's anchor minimum, so the slice encodes nothing."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    embeddings.clear_embedding_indexes()
    _encoder_present(monkeypatch)
    _seed_profiles(tmp_path)
    initialize_vault_state_offline(tmp_path, source="domain profiles small vault fixture")
    anchors = _plant_profile_chunks(tmp_path)
    assert 0 < anchors < ranking_config.DEFAULT_RANKING.working_set_semantic_min_population
    with principal.library_scope():
        yield tmp_path


def _activate(root: Path, turn: str, *, who=None, **arguments) -> dict:
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    arguments.setdefault("max_chars", 2000)
    with principal.request_scope(who or principal.owner_principal(surface="library")):
        return invoke_command(command, root, turn=turn, **arguments)


def _served(packet: dict) -> dict[str, list[str]]:
    """Each served domain's line texts, in served order."""
    return {entry["domain"]: [line["text"] for line in entry["lines"]] for entry in packet["domain_profiles"]["profiles"]}


def _cost(packet: dict, *domains: str) -> int:
    """What the named domains' lines occupy in the block's budget, as the hook renders them."""
    return sum(
        len(domain_profiles.item_line(entry["label"], line)) + 1
        for entry in packet["domain_profiles"]["profiles"] if entry["domain"] in domains for line in entry["lines"]
    )


def test_a_gaming_turn_serves_games_then_its_ancestor_and_no_music(profiles_vault) -> None:
    packet = _activate(profiles_vault, GAMES_TURN)

    assert packet["domain_profiles"]["state"] == "ready" and packet["domain_profiles"]["omitted"] == []
    assert _served(packet) == {"games": [RULE["games"]], "media": [RULE["media"]]}
    assert list(_served(packet)) == ["games", "media"]
    # No turn vector exists in this vault (too few anchors to calibrate one), and the block says it read words only.
    assert packet["domain_profiles"]["selection"] == "lexical"


def test_two_touched_domains_serve_their_shared_ancestor_once(profiles_vault) -> None:
    packet = _activate(profiles_vault, BOTH_TURN)

    domains = [entry["domain"] for entry in packet["domain_profiles"]["profiles"]]
    assert sorted(domains) == ["games", "media", "music"]
    assert domains[-1] != "games" and domains.count("media") == 1


def test_replacement_query_count_stays_fixed_across_profile_ancestors(profiles_vault, monkeypatch) -> None:
    domains = {}
    pages = []
    for index in range(12):
        domain = f"depth-{index}"
        domains[domain] = {"label": f"Depth {index}"}
        if index:
            domains[domain]["parent"] = f"depth-{index - 1}"
        path = _profile(profiles_vault, domain, [domain], _rule(domain, f"Calibration level {index}."))
        pages.append(domain_profiles.ProfilePage(path, path, domain))
    _write(profiles_vault, f"{KB}/_Schema/source-taxonomy.yaml", yaml.safe_dump({"schema_version": 1, "domains": domains}))
    _refresh(profiles_vault)
    taxonomy = source_taxonomy.load_taxonomy(profiles_vault)
    reading = domain_profiles.Reading(taxonomy, tuple(pages), ("depth-11",), profile_selection.LEXICAL)
    assert len(reading.domains()) == 12

    statements = []
    open_snapshot = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def trace_snapshot(index, **kwargs):
        connection = open_snapshot(index, **kwargs)
        if connection is not None:
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", trace_snapshot)
    with principal.request_scope(principal.owner_principal(surface="library")), principal.invocation_scope():
        caller = context_views.CallerView.current(profiles_vault)
        block = domain_profiles.compile_block(profiles_vault, caller, reading=reading)

    assert block["state"] == "ready"
    assert len([query for query in statements if "graph_edges" in query]) == 1


def test_a_general_turn_touches_no_profile(profiles_vault) -> None:
    packet = _activate(profiles_vault, GENERAL_TURN)

    assert packet["domain_profiles"] == {"state": "empty", "profiles": [], "omitted": [], "selection": "lexical"}


def test_an_unresolved_topic_supplies_nothing_and_never_becomes_global(profiles_vault) -> None:
    _profile(profiles_vault, "silent", ["silent-cinema"], _rule("silent", "Silent features only, never talkies."))
    _refresh(profiles_vault)

    packet = _activate(profiles_vault, "Which silent features suit tonight")

    assert "silent" not in json.dumps(packet["domain_profiles"]) and "silent" not in json.dumps(packet["stance"])
    assert [item["text"] for item in packet["stance"]["items"]] == [RULE["global"]]


def test_a_vault_keeps_the_most_domains_its_limit_names_and_their_ancestors(profiles_vault) -> None:
    named = {"games", "music", "cooking"}
    two = set(_served(_activate(profiles_vault, THREE_TURN)))
    _set_budgets(profiles_vault, profile_domains_max=3)

    three = set(_served(_activate(profiles_vault, THREE_TURN)))

    assert len(named & two) == 2 and "media" in two
    assert named <= three and "media" in three


def test_over_budget_keeps_the_strongest_and_lists_the_rest_to_read_on_demand(profiles_vault) -> None:
    roomy = _activate(profiles_vault, BOTH_TURN)
    served = [entry["domain"] for entry in roomy["domain_profiles"]["profiles"]]
    _set_budgets(profiles_vault, profiles_max_chars=_cost(roomy, *served[:2]))

    tight = _activate(profiles_vault, BOTH_TURN)

    assert list(_served(tight)) == served[:2]
    assert tight["domain_profiles"]["omitted"] == [
        {"domain": served[2], "label": served[2].title(), "ref": f"{PROFILES}/{served[2]}.md"},
    ]


def test_a_held_profile_is_unchanged_and_spends_no_budget(profiles_vault) -> None:
    roomy = _activate(profiles_vault, GAMES_TURN)
    _set_budgets(profiles_vault, profiles_max_chars=_cost(roomy, "games"))
    first = _activate(profiles_vault, GAMES_TURN)

    second = _activate(profiles_vault, GAMES_TURN, continuity=first["continuity"])

    assert list(_served(first)) == ["games"] and [item["domain"] for item in first["domain_profiles"]["omitted"]] == ["media"]
    held, sent = second["domain_profiles"]["profiles"]
    assert (held["domain"], held["state"], held["lines"]) == ("games", "unchanged", [])
    assert held["generation"] == first["domain_profiles"]["profiles"][0]["generation"]
    assert (sent["domain"], sent["state"]) == ("media", "ready") and second["domain_profiles"]["omitted"] == []


def test_an_edited_profile_is_sent_again_under_a_new_generation(profiles_vault) -> None:
    first = _activate(profiles_vault, GAMES_TURN)
    _profile(profiles_vault, "games", ["games"], _rule("games", "Tactical cooperative play, and tell me the player count."))
    _refresh(profiles_vault)

    second = _activate(profiles_vault, GAMES_TURN, continuity=first["continuity"])

    before = {entry["domain"]: entry for entry in first["domain_profiles"]["profiles"]}
    after = {entry["domain"]: entry for entry in second["domain_profiles"]["profiles"]}
    assert after["games"]["state"] == "ready" and after["games"]["generation"] != before["games"]["generation"]
    assert after["games"]["lines"][0]["text"].endswith("the player count.")
    assert after["media"]["state"] == "unchanged"


def test_profiles_leave_the_turns_allowance_whole(profiles_vault, monkeypatch) -> None:
    on = _activate(profiles_vault, GAMES_TURN, max_chars=700)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PROFILES", "off")
    working_set_runtime.reset_caches_for_tests()
    off = _activate(profiles_vault, GAMES_TURN, max_chars=700)

    assert on["domain_profiles"]["state"] == "ready" and "domain_profiles" not in off
    assert on["budget"]["used_chars"] == off["budget"]["used_chars"] <= 700
    assert on["map"] == off["map"] and on["turn_slice"] == off["turn_slice"]
    assert on["task_context"]["candidates"] == off["task_context"]["candidates"]


def test_a_profile_line_naming_a_hidden_page_is_withheld_without_a_trace(profiles_vault) -> None:
    hidden = _page(profiles_vault, "hidden", body="- [rule] Secret instruction. ^secret\n")
    _withhold(profiles_vault, hidden, ceiling=0)
    _refresh(profiles_vault)
    plain = _activate(profiles_vault, GAMES_TURN, who=_external())
    _profile(profiles_vault, "games", ["games"], _rule("games") + f"- [rule] Follow [[{hidden.removesuffix('.md')}]]. ^link\n")
    _refresh(profiles_vault)

    linked = _activate(profiles_vault, GAMES_TURN, who=_external())

    assert _served(plain) == {"games": [RULE["games"]], "media": [RULE["media"]]}
    assert linked["domain_profiles"] == plain["domain_profiles"]
    assert "hidden" not in json.dumps(linked["domain_profiles"])

    _profile(profiles_vault, "games", ["games"], f"- [rule] Follow [[{hidden.removesuffix('.md')}]]. ^link\n")
    _refresh(profiles_vault)
    only_hidden = _activate(profiles_vault, GAMES_TURN, who=_external())["domain_profiles"]

    assert [entry["domain"] for entry in only_hidden["profiles"]] == ["media"] and only_hidden["omitted"] == []


def test_a_retyped_global_profile_serves_the_same_stance_lines(profiles_vault) -> None:
    (profiles_vault / PROFILES / "global.md").unlink()
    _page(profiles_vault, "global", "stance: true\n", body=_rule("global"))
    _refresh(profiles_vault)
    marked = _activate(profiles_vault, GENERAL_TURN)["stance"]
    (profiles_vault / KB / "Notes" / "Insights" / "global.md").unlink()
    _profile(profiles_vault, "global", None, _rule("global"))
    _refresh(profiles_vault)

    retyped = _activate(profiles_vault, GENERAL_TURN)["stance"]

    assert [item["text"] for item in marked["items"]] == [RULE["global"]]
    assert [item["text"] for item in retyped["items"]] == [item["text"] for item in marked["items"]]


# --------------------------------------------------------------------------- #
# The continuity token and the catalogue
# --------------------------------------------------------------------------- #


def _tampered(**fields: object) -> str:
    token = working_set_runtime.encode_continuity(
        identity="i", roles_hash="r", conventions_hash="c", generation=0, refs=[], roles=[],
    )
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    packed = json.dumps({**payload, **fields}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


@pytest.mark.parametrize(
    "malformed",
    ["text", 7, ["games"], {"games": 3}, {"x" * 5000: "g"}],
    ids=["string", "number", "list", "non-string generation", "oversized key"],
)
def test_a_malformed_profile_generation_map_reads_as_holding_nothing(malformed) -> None:
    decoded = working_set_runtime.decode_continuity(_tampered(profile_generations=malformed))

    assert decoded is not None and decoded["profile_generations"] == {}


def test_sixteen_profile_generations_fit_the_token_and_more_are_cut() -> None:
    longest = {f"{n:02d}-" + "d" * 61: "g" * 64 for n in range(working_set_runtime.CONTINUITY_MAX_PROFILES)}

    token = working_set_runtime.encode_continuity(
        identity="i", roles_hash="r", conventions_hash="c", generation=0, refs=[], roles=[],
        profile_generations=longest,
    )
    flooded = working_set_runtime.decode_continuity(_tampered(profile_generations={f"d{n}": "g" for n in range(40)}))

    assert len(token) <= working_set_runtime.CONTINUITY_MAX_CHARS
    assert working_set_runtime.decode_continuity(token)["profile_generations"] == longest
    assert len(flooded["profile_generations"]) == working_set_runtime.CONTINUITY_MAX_PROFILES


def test_the_oldest_held_profiles_drop_first() -> None:
    limit = working_set_runtime.CONTINUITY_MAX_PROFILES
    held = {f"old-{n}": "g" for n in range(limit)}
    served = {"profiles": [{"domain": "fresh", "generation": "h", "state": "ready"}]}

    kept = working_set_runtime.held_profile_generations(served, held)

    assert list(kept) == ["fresh", *list(held)[: limit - 1]]


def test_a_catalogue_from_before_profiles_rebuilds_and_lists_them(profiles_vault) -> None:
    catalogue = context_catalogue.ContextCatalogue(profiles_vault)
    catalogue.close()
    db = sqlite3.connect(catalogue.path)
    db.execute("DROP INDEX entries_profile")
    db.execute("ALTER TABLE entries DROP COLUMN profile")
    db.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    db.commit()
    db.close()

    rebuilt = _built(profiles_vault)
    try:
        listed = {entry.path.rsplit("/", 1)[-1] for entry in rebuilt.entries_by_id(rebuilt.profile_ids())}
    finally:
        rebuilt.close()

    assert listed == {"global.md", "media.md", "games.md", "music.md", "cooking.md"}


# --------------------------------------------------------------------------- #
# Selection by meaning
# --------------------------------------------------------------------------- #


def test_a_turn_that_names_no_profile_word_but_means_the_games_profile_selects_it(semantic_vault) -> None:
    packet = _activate(semantic_vault, PARAPHRASE_TURN)

    assert packet["domain_profiles"]["selection"] == "semantic"
    assert _served(packet) == {"games": [RULE["games"]], "media": [RULE["media"]]}


def test_a_general_knowledge_turn_selects_no_profile_even_when_both_readings_run(semantic_vault, monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: _random_unit("general " + text))

    packet = _activate(semantic_vault, GENERAL_TURN)

    assert packet["domain_profiles"] == {"state": "empty", "profiles": [], "omitted": [], "selection": "semantic"}


def test_a_small_catalogue_selects_by_meaning_at_the_shipped_floor_from_the_vector_the_task_lane_encoded(
    small_semantic_vault,
) -> None:
    assert activation_conventions.load_conventions(small_semantic_vault).conventions.budget("profile_lexical_floor") == 8.5

    packet = _activate(small_semantic_vault, PARAPHRASE_TURN)

    assert packet["domain_profiles"]["selection"] == "semantic"
    assert _served(packet) == {"games": [RULE["games"]], "media": [RULE["media"]]}


@pytest.mark.parametrize("vault_fixture", ["semantic_vault", "small_semantic_vault"])
def test_selecting_profiles_encodes_the_turn_no_more_often_than_without_them(request, monkeypatch, vault_fixture) -> None:
    root = request.getfixturevalue(vault_fixture)
    calls: list[str] = []
    original = embeddings.embed_activation_query_if_loaded
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: calls.append(text) or original(text))
    _activate(root, PARAPHRASE_TURN)
    with_profiles = len(calls)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PROFILES", "off")
    calls.clear()
    _activate(root, PARAPHRASE_TURN)

    assert with_profiles == len(calls) > 0


def test_without_a_turn_vector_the_paraphrase_selects_nothing_and_the_block_says_it_read_words_only(
    semantic_vault, monkeypatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    packet = _activate(semantic_vault, PARAPHRASE_TURN)

    assert packet["domain_profiles"] == {"state": "empty", "profiles": [], "omitted": [], "selection": "lexical"}


def test_a_vault_floor_decides_whether_a_meaning_match_selects(semantic_vault) -> None:
    _set_budgets(semantic_vault, profile_semantic_floor=0.9, profile_lexical_floor=8.5)

    packet = _activate(semantic_vault, PARAPHRASE_TURN)

    assert packet["domain_profiles"]["profiles"] == [] and packet["domain_profiles"]["selection"] == "semantic"


def test_selecting_profiles_walks_the_vault_no_more_often_than_without_them(profiles_vault, monkeypatch) -> None:
    """The keyword reading takes the checkpoint the request holds; working one out walks every page of the vault."""
    walks: list[None] = []
    original = recall_policy.iter_recall_markdown
    monkeypatch.setattr(recall_policy, "iter_recall_markdown", lambda *args, **kwargs: walks.append(None) or original(*args, **kwargs))
    _activate(profiles_vault, GAMES_TURN)  # the first request fills the caches; count the second of each kind
    walks.clear()
    packet = _activate(profiles_vault, GAMES_TURN)
    with_profiles = len(walks)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PROFILES", "off")
    walks.clear()
    _activate(profiles_vault, GAMES_TURN)

    assert packet["domain_profiles"]["selection"] == "lexical" and "games" in _served(packet)
    assert with_profiles == len(walks)


def _keyword_reading_down(*args, **kwargs):
    raise OSError("lexical index unreadable")


def _keyword_catalogue_behind(*args, **kwargs):
    return lexstore.CatalogQueryResult(None, lexstore.CatalogReadiness("stale", False, "auto"))


@pytest.mark.parametrize("keyword_reading", [_keyword_reading_down, _keyword_catalogue_behind])
def test_a_keyword_reading_that_does_not_run_with_no_turn_vector_leaves_the_profiles_unknown_not_absent(
    profiles_vault, monkeypatch, keyword_reading,
) -> None:
    # Only the profile selection's own reading fails; the turn slice and task context keep theirs.
    monkeypatch.setattr(profile_selection, "lexstore", SimpleNamespace(search_bm25_result=keyword_reading))

    packet = _activate(profiles_vault, GAMES_TURN)

    assert packet["domain_profiles"] == {"state": "unavailable", "profiles": [], "omitted": None, "selection": "none"}


# --------------------------------------------------------------------------- #
# The hook and the budgets
# --------------------------------------------------------------------------- #


def test_the_hook_renders_each_served_profile_line_whole_beside_an_unchanged_working_set(profiles_vault, monkeypatch) -> None:
    """A real packet: a profile that took the hook's room would cut the map or the slice, and a cut line is lost."""
    on = _activate(profiles_vault, GAMES_TURN)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PROFILES", "off")
    working_set_runtime.reset_caches_for_tests()
    off = _activate(profiles_vault, GAMES_TURN)
    # A ceiling the working set just fits: a profile charged to it would have to push the map or the slice out.
    cap = len(hook._format_map_with_slice(off, 4000, "")[0])

    on_text, on_complete, *_ = hook._format_map_with_slice(on, cap, "")
    off_text, off_complete, *_ = hook._format_map_with_slice(off, cap, "")

    on_lines = on_text.splitlines()
    rendered = [line for line in on_lines if line.startswith("- profile ")]
    assert rendered == [
        domain_profiles.item_line(entry["label"], line)
        for entry in on["domain_profiles"]["profiles"] for line in entry["lines"]
    ] and len(rendered) == 2
    assert [line for line in on_lines if not line.startswith("- profile")] == off_text.splitlines()
    assert on_complete == off_complete


def test_the_hook_bound_is_the_largest_one_a_vault_may_set() -> None:
    """The hook cannot import the registry, so the largest vault bound is mirrored and pinned here."""
    assert hook._PROFILES_MAX_CHARS == activation_conventions.BUDGET_BOUNDS["profiles_max_chars"][1]
