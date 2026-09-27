"""A correction in one session teaches the next (close-memory-loop step 5,
lane B, task B7).

End to end through the tools: a turn misses, the agent picks the page the
user meant, the pick carries a learning advisory, the agent acts on it through
the writer the advisory names, and a FRESH session (new process caches, no
continuity token, another session key) resolves the user's words. Reverting
the learned entry restores the earlier behaviour, and the soundness negatives
stay negative with a learned name and a learned cue present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_context_activation_resolver_soundness import _live_hot_profile, _statuses
from test_governance_egress import _reset_caches
from test_working_set_carry import _seed_carry_pages
from test_working_set_hot_projection import _live, _one_old_tick
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    activation_conventions,
    capture_sweep,
    commands,
    lexstore,
    working_set,
    working_set_heat,
    working_set_index,
    working_set_runtime,
    writer_lease,
)

BENCHMARKS_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
if str(BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS_DIR))

from epistemic.corpora.context_activation import (  # noqa: E402
    C10_TURN,
    T10_TURN,
    T11_TURN,
    build_soundness_probe_corpus,
)

SLED = "Knowledge Base/Products/Cargo Sled.md"


@pytest.fixture
def journey_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    activation_conventions.clear_cache()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: True)
    return vault


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _edit(vault: Path, rel: str, operation: dict, why: str = "journey") -> dict:
    return writer_lease.invoke_command(
        _command("edit_memory"), vault, path=rel, why=why, operation=operation
    )


def _fresh_session() -> None:
    """What a new process on another client starts from: no packet cache, no
    memoised registry, no token."""
    working_set_runtime.reset_caches_for_tests()
    activation_conventions.clear_cache()


def _resolved(packet: dict) -> list[str]:
    return [item["path"] for item in packet["anchors"] if item["status"] == "resolved"]


def test_a_correction_teaches_a_name_the_next_session_uses(journey_vault: Path) -> None:
    turn = "wie geht es dem Schlitten"
    first = commands.op_activate_context(journey_vault, turn=turn, session="session-one")
    assert _resolved(first) == []

    picked = commands.op_activate_context(
        journey_vault, turn=turn, anchor=SLED, session="session-one"
    )
    advisory = picked["learning"]
    name = next(option for option in advisory["options"] if option["family"] == "name")
    assert "schlitten" in advisory["turn_terms"]

    _edit(
        journey_vault,
        name["path"],
        {
            "kind": "patch_frontmatter",
            "field": name["field"],
            "value": [*name["current"], "Schlitten"],
            "expected_hash": name["expected_hash"],
        },
        why="the user calls the cargo sled 'Schlitten'",
    )
    history = commands.op_read_memory(journey_vault, path=SLED, include_history=True)
    assert any("Schlitten" in entry["summary"] for entry in history["history"])

    _fresh_session()
    packet = commands.op_activate_context(journey_vault, turn=turn, session="session-two")
    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])
    assert "exact_alias" in packet["anchors"][0]["evidence"]


def test_a_correction_teaches_a_referential_cue_in_another_language(journey_vault: Path) -> None:
    _edit(
        journey_vault,
        SLED,
        {"kind": "patch_frontmatter", "field": "last_checked", "value": "2026-09-20"},
    )
    turn = "on reprend ?"
    assert _resolved(commands.op_activate_context(journey_vault, turn=turn)) == []

    advisory = commands.op_activate_context(journey_vault, turn=turn, anchor=SLED)["learning"]
    cue = next(option for option in advisory["options"] if option["family"] == "referential_cue")
    proposal = {
        "schema_version": 1,
        "referential": {
            "add_cues": [
                {
                    "value": "on reprend",
                    "why": "the user resumes work in French",
                    "at": "2026-09-25",
                    "evidence": advisory["review"],
                }
            ]
        },
    }
    validated = commands.op_schema_memory(
        journey_vault,
        subject=cue["subject"],
        operation="validate",
        proposal=proposal,
    )
    assert validated["valid"] is True, validated["findings"]
    commands.op_schema_memory(
        journey_vault,
        subject=cue["subject"],
        operation=cue["operation"],
        proposal=proposal,
        why="the user resumes work in French",
        expected_hash=cue["expected_hash"],
    )

    _fresh_session()
    packet = commands.op_activate_context(journey_vault, turn=turn)
    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])
    assert "recency" in packet["anchors"][0]["evidence"]
    # The cue only points back: said with a name, the name decides.
    named = commands.op_activate_context(journey_vault, turn="on reprend Marit Solheim")
    assert _resolved(named) == ["Knowledge Base/Entities/People/Marit Solheim.md"]


def test_reverting_a_learned_name_restores_abstention(journey_vault: Path) -> None:
    turn = "wie geht es dem Schlitten"
    name = next(
        option
        for option in commands.op_activate_context(journey_vault, turn=turn, anchor=SLED)[
            "learning"
        ]["options"]
        if option["family"] == "name"
    )
    _edit(
        journey_vault,
        SLED,
        {
            "kind": "patch_frontmatter",
            "field": "learned_aliases",
            "value": ["Schlitten"],
            "expected_hash": name["expected_hash"],
        },
    )
    _fresh_session()
    assert _resolved(commands.op_activate_context(journey_vault, turn=turn)) == [SLED]

    _edit(
        journey_vault,
        SLED,
        {"kind": "patch_frontmatter", "field": "learned_aliases", "value": []},
        why="the user meant a different sled",
    )
    _fresh_session()
    packet = commands.op_activate_context(journey_vault, turn=turn)
    assert _resolved(packet) == []
    assert packet["abstained"] is True


def test_reverting_a_learned_cue_restores_the_earlier_reading(journey_vault: Path) -> None:
    _edit(
        journey_vault,
        SLED,
        {"kind": "patch_frontmatter", "field": "last_checked", "value": "2026-09-20"},
    )
    commands.op_schema_memory(
        journey_vault,
        subject="activation-conventions",
        operation="save-conventions",
        proposal={"schema_version": 1, "referential": {"add_cues": ["weiter"]}},
        why="the user resumes in German",
        expected_hash=activation_conventions.load_conventions(journey_vault).content_hash,
    )
    _fresh_session()
    assert _resolved(commands.op_activate_context(journey_vault, turn="weiter")) == [SLED]

    version = commands.op_schema_memory(
        journey_vault, subject="activation-conventions", operation="history"
    )["versions"][0]["version"]
    commands.op_schema_memory(
        journey_vault,
        subject="activation-conventions",
        operation="restore",
        version=version,
        why="the cue was a one-off",
        expected_hash=activation_conventions.load_conventions(journey_vault).content_hash,
    )
    _fresh_session()
    assert _resolved(commands.op_activate_context(journey_vault, turn="weiter")) == []


# --------------------------------------------------------------------------- #
# The soundness negatives with a learned name and a learned cue present
# --------------------------------------------------------------------------- #


def _learned_on(vault: Path, rel: str) -> None:
    """One learned name on `rel` and one learned cue, both through the tools."""
    text = (vault / rel).read_text(encoding="utf-8")
    from exomem.vault import content_hash

    _edit(
        vault,
        rel,
        {
            "kind": "patch_frontmatter",
            "field": "learned_aliases",
            "value": ["Zugwegknoten"],
            "expected_hash": content_hash(text),
        },
    )
    commands.op_schema_memory(
        vault,
        subject="activation-conventions",
        operation="save-conventions",
        proposal={"schema_version": 1, "referential": {"add_cues": ["weiter"]}},
        why="the user resumes in German",
        expected_hash=activation_conventions.load_conventions(vault).content_hash,
    )
    activation_conventions.clear_cache()


def test_the_negative_twins_stay_negative_with_learned_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = build_soundness_probe_corpus(tmp_path)
    vault = tmp_path
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    _learned_on(vault, manifest.hub_paths[1])
    working_set_index.WorkingSetIndex(vault).rebuild()
    _live_hot_profile(vault, freshest=manifest.hub_paths[0])
    working_set_runtime.reset_caches_for_tests()

    learned_cue = working_set.compile_packet(vault, turn="weiter", max_chars=4000)
    assert _statuses(learned_cue).get(manifest.hub_paths[0]) == "resolved", (
        "the learned cue must be live, or the negatives below prove nothing"
    )
    learned_name = working_set.compile_packet(vault, turn="der Zugwegknoten", max_chars=4000)
    assert _statuses(learned_name).get(manifest.hub_paths[1]) == "resolved"

    for turn in (T10_TURN, T11_TURN, f"weiter {T10_TURN}"):
        packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)
        assert packet["abstained"] is True, turn
        assert packet["abstention"] == {"reason": "unresolved"}, turn
        assert all("recency" not in item["evidence"] for item in packet["anchors"]), turn

    one_word = working_set.compile_packet(vault, turn=C10_TURN, max_chars=4000)
    assert _statuses(one_word).get(manifest.bike_path) == "resolved"
    assert all("recency" not in item["evidence"] for item in one_word["anchors"])
