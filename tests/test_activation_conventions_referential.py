"""The `referential` section of the activation conventions (close-memory-loop
step 5, lane B, task B1).

The words that make a turn point back at recent work ("continue", "where
were we") used to be an English list in product code. They are now a
shipped seed in the scaffold registry that a vault override extends with
`add_cues`/`add_filler` and narrows with `drop_cues`/`drop_filler`, through
the same governed save as every other convention. A cue change is a TURN
change only: it joins the packet cache key through its own digest and never
rebuilds the anchor sidecar or strands a continuity token.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from test_governance_egress import _reset_caches
from test_working_set_carry import _seed_carry_pages
from test_working_set_hot_projection import SLED, _edit, _live, _one_old_tick, _resolved
from test_working_set_index import _seed_planning, _seed_structure

from exomem import activation_conventions as ac
from exomem import (
    commands,
    lexstore,
    working_set_heat,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)

#: U2's lists as they stood in `working_set_resolve` before this change,
#: restated here so the seed is pinned against the reviewed values rather
#: than against itself.
PREVIOUS_CUES = (
    "continue",
    "where were we",
    "where did we leave",
    "what were we doing",
    "carry on",
    "status",
    "status update",
    "status report",
    "what's next",
    "whats next",
    "what is next",
    "same as before",
    "as before",
    "pick up where",
    "resume",
)
PREVIOUS_FILLER = frozenset(
    {
        "ok", "okay", "so", "now", "let's", "lets", "please",
        "work", "task", "thing", "things", "stuff", "it", "this", "that",
        "pending", "left", "off", "up", "from", "where", "what", "what's", "whats",
        "here", "today", "yesterday", "last", "stopped", "doing", "on", "with", "again",
    }
)


def _save(vault: Path, referential: dict, *, why: str = "learned from a correction") -> dict:
    current = ac.load_conventions(vault)
    return commands.op_schema_memory(
        vault,
        operation="save-conventions",
        subject="activation-conventions",
        proposal={"schema_version": 1, "referential": referential},
        why=why,
        expected_hash=current.content_hash,
    )


def _validate(referential: dict) -> list[dict]:
    result = commands.op_schema_memory(
        Path("."),
        operation="validate",
        subject="activation-conventions",
        proposal={"schema_version": 1, "referential": referential},
    )
    return result["findings"]


@pytest.fixture
def heat_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Local copy of `test_working_set_hot_projection.heat_vault` (a
    cross-file fixture import reads as a redefinition to ruff F811): a warm
    vault whose every page was written once, long ago, with a live registry."""
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    _one_old_tick(vault)
    _live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


@pytest.fixture(autouse=True)
def _fresh_registry():
    ac.clear_cache()
    yield
    ac.clear_cache()


# --------------------------------------------------------------------------- #
# The shipped seed
# --------------------------------------------------------------------------- #


def test_the_shipped_seed_equals_the_previous_referential_lists() -> None:
    shipped = ac.shipped_conventions().conventions
    assert shipped.referential_cues == PREVIOUS_CUES
    assert shipped.referential_filler == PREVIOUS_FILLER
    raw = yaml.safe_load(ac.shipped_conventions_text())["referential"]
    assert tuple(raw["cues"]) == PREVIOUS_CUES
    assert frozenset(raw["filler"]) == PREVIOUS_FILLER


def _string_collections(tree: ast.AST) -> list[tuple[int, list[str]]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            strings = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if strings:
                found.append((node.lineno, strings))
    return found


@pytest.mark.parametrize("module", ["working_set_resolve.py", "working_set.py"])
def test_no_referential_word_list_remains_in_product_code(module: str) -> None:
    """The owner's rule, executable: a referential cue or filler word is vault
    data, so no string-literal collection in the compiler may carry one."""
    source = Path(working_set_resolve.__file__).with_name(module).read_text(encoding="utf-8")
    words = set(PREVIOUS_CUES) | PREVIOUS_FILLER
    offending = [
        (line, strings)
        for line, strings in _string_collections(ast.parse(source))
        if len(set(strings) & words) >= 3
    ]
    assert offending == []


# --------------------------------------------------------------------------- #
# The override grammar
# --------------------------------------------------------------------------- #


def test_an_added_cue_makes_a_turn_referential(tmp_path: Path) -> None:
    assert not working_set_resolve.analyze_turn("weiter bitte").referential
    registry = ac.load_conventions(
        proposal={
            "schema_version": 1,
            "referential": {"add_cues": ["weiter"], "add_filler": ["bitte"]},
        }
    )
    assert registry.findings == ()
    vocabulary = registry.conventions.referential

    assert working_set_resolve.analyze_turn("weiter bitte", vocabulary=vocabulary).referential
    assert working_set_resolve.analyze_turn("weiter", vocabulary=vocabulary).referential
    # A shipped cue still works beside the learned one.
    assert working_set_resolve.analyze_turn("continue", vocabulary=vocabulary).referential


@pytest.mark.parametrize("cue", ["the", "it", "ok so", "where were we"])
def test_an_added_cue_needs_a_word_of_its_own(cue: str) -> None:
    """A cue made only of function words or filler adds nothing a filler word
    does not, and an exact-alias-shaped cue of "it" would point every turn
    back. "where were we" is a SHIPPED cue of only function words: shipped
    entries are the reviewed seed, added ones must earn their place."""
    findings = _validate({"add_cues": [cue]})
    assert [finding["code"] for finding in findings] == ["cue_without_content"]


def test_a_filler_entry_is_one_token() -> None:
    findings = _validate({"add_filler": ["bitte schön"]})
    assert [finding["code"] for finding in findings] == ["filler_not_one_token"]
    assert _validate({"add_filler": ["bitte"]}) == []


def test_grammar_bounds_and_provenance_keys_are_findings() -> None:
    too_long = "x" * (ac.MAX_ENTRY_CHARS + 1)
    assert [f["code"] for f in _validate({"add_cues": [too_long]})] == ["entry_too_long"]
    assert [f["code"] for f in _validate({"drop_cues": ["not a shipped cue"]})] == [
        "drop_not_shipped"
    ]
    assert [f["code"] for f in _validate({"drop_filler": ["bitte"]})] == ["drop_not_shipped"]
    assert [
        f["code"]
        for f in _validate({"add_cues": [{"value": "weiter", "who": "someone"}]})
    ] == ["invalid_provenance"]
    assert [f["code"] for f in _validate({"remove_cues": ["status"]})] == ["unknown_field"]
    many = [f"weiter{index}" for index in range(ac.MAX_ADDED_CUES + 1)]
    assert [f["code"] for f in _validate({"add_cues": many})] == ["cap_exceeded"]


def test_dropping_a_shipped_cue_narrows_referential_turns() -> None:
    assert working_set_resolve.analyze_turn("status").referential
    registry = ac.load_conventions(
        proposal={"schema_version": 1, "referential": {"drop_cues": ["status"]}}
    )
    assert registry.findings == ()
    vocabulary = registry.conventions.referential

    assert not working_set_resolve.analyze_turn("status", vocabulary=vocabulary).referential
    assert not working_set_resolve.analyze_turn("status?", vocabulary=vocabulary).referential
    # Only the dropped phrase goes; its longer siblings stay.
    assert working_set_resolve.analyze_turn("status update", vocabulary=vocabulary).referential


# --------------------------------------------------------------------------- #
# The digest split
# --------------------------------------------------------------------------- #


def test_a_cue_change_changes_only_the_turn_digest() -> None:
    shipped = ac.shipped_conventions()
    learned = ac.load_conventions(
        proposal={"schema_version": 1, "referential": {"add_cues": ["weiter"]}}
    )
    assert learned.conventions_hash == shipped.conventions_hash
    assert learned.turn_hash != shipped.turn_hash
    assert learned.content_hash != shipped.content_hash
    stopword = ac.load_conventions(
        proposal={"schema_version": 1, "stopwords": {"add": ["zzzz"]}}
    )
    assert stopword.conventions_hash != shipped.conventions_hash
    assert stopword.turn_hash == shipped.turn_hash


def test_a_cue_change_wipes_no_sidecar_and_strands_no_token(heat_vault: Path) -> None:
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    first = commands.op_activate_context(heat_vault, turn="weiter")
    assert first["abstained"] is True or _resolved(first) != [SLED]
    token = commands.op_activate_context(heat_vault, turn="tell me about the Cargo Sled")[
        "continuity"
    ]
    index = working_set_index.WorkingSetIndex(heat_vault)
    generation = index.generation()
    sidecar_token = index.token()

    _save(heat_vault, {"add_cues": ["weiter"]})

    after = commands.op_activate_context(heat_vault, turn="weiter", continuity=token)
    assert _resolved(after) == [SLED], (after.get("abstention"), after["anchors"])
    assert after["generation"]["continuity"] != working_set_runtime.CONTINUITY_STALE
    assert after["generation"]["conventions_turn_hash"] == ac.load_conventions(
        heat_vault
    ).turn_hash
    assert index.generation() == generation
    assert index.token() == sidecar_token


def test_the_packet_cache_key_carries_the_turn_digest(heat_vault: Path) -> None:
    """Deliberately no cache reset between the two calls: the key alone must
    turn the second request into a miss."""
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    before = commands.op_activate_context(heat_vault, turn="weiter")
    _save(heat_vault, {"add_cues": ["weiter"]})
    after = commands.op_activate_context(heat_vault, turn="weiter")
    assert _resolved(before) == []
    assert _resolved(after) == [SLED]


# --------------------------------------------------------------------------- #
# Soundness and provenance
# --------------------------------------------------------------------------- #


def test_a_learned_cue_never_answers_a_turn_that_names_an_anchor(heat_vault: Path) -> None:
    """The recency exception and nothing wider: a learned cue joins only the
    cue set inside the referential test. A turn that also names an anchor has
    residue, so it is not referential, and the named anchor wins."""
    marit = "Knowledge Base/Entities/People/Marit Solheim.md"
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    _save(heat_vault, {"add_cues": ["weiter"], "add_filler": ["bitte"]})

    named = commands.op_activate_context(heat_vault, turn="weiter mit Marit Solheim bitte")
    assert _resolved(named) == [marit], (named.get("abstention"), named["anchors"])
    assert all("recency" not in item["evidence"] for item in named["anchors"])
    # A cue spoken in its ordinary sense with anything else said: no prior.
    ordinary = commands.op_activate_context(heat_vault, turn="weiter zum Bahnhof")
    assert SLED not in _resolved(ordinary)


def test_learned_entries_keep_their_provenance(heat_vault: Path) -> None:
    entry = {
        "value": "on reprend",
        "why": "user resumes in French",
        "at": "2026-09-23",
        "evidence": "activation-naming:example",
    }
    _save(heat_vault, {"add_cues": ["weiter", entry]})

    registry = ac.load_conventions(heat_vault)
    assert registry.findings == ()
    assert "on reprend" in registry.conventions.referential_cues
    provenance = {item["value"]: item for item in registry.conventions.referential_provenance}
    assert provenance["on reprend"] == {"field": "add_cues", **entry}
    assert provenance["weiter"] == {"field": "add_cues", "value": "weiter"}
    # The saved file round-trips the mapping, so one learned cue can be
    # dropped later without reverting the whole file.
    saved = yaml.safe_load(ac.override_path(heat_vault).read_text(encoding="utf-8"))
    assert entry in saved["referential"]["add_cues"]
    assert working_set_resolve.analyze_turn(
        "on reprend", vocabulary=registry.conventions.referential
    ).referential
