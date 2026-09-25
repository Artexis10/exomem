"""Learning from the agent's corrections (close-memory-loop step 5, lane B,
tasks B4 and B5).

The sensor is the agent's pick. When the user corrects which page they meant,
the agent calls `activate_context` again with the same turn and `anchor=` the
page. At that seam, after the guard admitted the choice, the server classifies
the pick from facts the request already holds, counts a miss by path and class
(never a word of the turn), and may attach ONE bounded advisory naming the
existing writers that would teach the vault the user's words. The advisory
writes nothing and grants nothing; the writers' own hash guards refuse a stale
one.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest
from test_governance_egress import _reset_caches
from test_working_set_carry import CARRY_PAGE, _seed_carry_pages
from test_working_set_hot_projection import _live, _one_old_tick
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    activation_conventions,
    capture_sweep,
    commands,
    lexstore,
    review_state,
    working_set_heat,
    working_set_index,
    working_set_learning,
    working_set_runtime,
    writer_lease,
)
from exomem.vault import content_hash

SLED = "Knowledge Base/Products/Cargo Sled.md"
MARIT = "Knowledge Base/Entities/People/Marit Solheim.md"
NAMELESS = "wie geht es dem Schlitten"
HOUR_NS = 3600 * 10**9


@pytest.fixture
def learning_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A warm vault written once long ago, with a live registry, proactive
    capture permitted, and the Cargo Sled the last page the user worked on."""
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
    _work(vault, SLED)
    return vault


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _work(vault: Path, rel: str, value: str = "2026-09-20") -> dict:
    return writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=rel,
        why="learning test",
        operation={"kind": "patch_frontmatter", "field": "last_checked", "value": value},
    )


def _pick(vault: Path, turn: str, rel: str, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, anchor=rel, **kwargs)


def _misses(vault: Path) -> dict[tuple[str, str], int]:
    return {(row.path, row.klass): row.count for row in working_set_learning.misses(vault)}


def _later(monkeypatch: pytest.MonkeyPatch, hours: float) -> None:
    """Move the sensor's clock forward, as a later session would find it."""
    offset = int(hours * HOUR_NS) + working_set_learning._CLOCK_OFFSET_NS
    monkeypatch.setattr(working_set_learning, "_CLOCK_OFFSET_NS", offset)


# --------------------------------------------------------------------------- #
# B4: the sensor and its counter
# --------------------------------------------------------------------------- #


def test_a_pick_whose_turn_never_named_the_anchor_is_a_naming_miss(learning_vault: Path) -> None:
    packet = _pick(learning_vault, NAMELESS, SLED)

    assert packet["abstained"] is False
    assert packet["learning"]["observed"]["class"] == "naming"
    assert _misses(learning_vault) == {(SLED, "naming"): 1}


def test_a_pick_that_disambiguates_is_not_a_miss(learning_vault: Path) -> None:
    packet = _pick(learning_vault, "what are the constraints of the Cargo Sled", SLED)

    assert "learning" not in packet
    assert _misses(learning_vault) == {}


def test_a_pick_of_a_page_outside_the_profile_after_a_named_turn_is_not_a_miss(
    learning_vault: Path,
) -> None:
    packet = _pick(learning_vault, "tell me about Marit Solheim", MARIT)

    assert "learning" not in packet
    assert _misses(learning_vault) == {}


def test_a_pick_of_a_recent_page_after_a_nameless_turn_is_a_cue_miss(learning_vault: Path) -> None:
    """The research note is no anchor, so no name can be learned for it; it
    is the page the user just worked on, so the turn pointed at recent work in
    words the vault does not know."""
    _work(learning_vault, CARRY_PAGE)

    packet = _pick(learning_vault, "weiter bitte", CARRY_PAGE)

    assert packet["learning"]["observed"]["class"] == "cue"
    assert [option["family"] for option in packet["learning"]["options"]] == ["referential_cue"]
    assert _misses(learning_vault) == {(CARRY_PAGE, "cue"): 1}


def test_a_referential_pick_moves_heat_without_a_miss(learning_vault: Path) -> None:
    before = commands.op_activate_context(learning_vault, turn="continue")
    assert [item["path"] for item in before["anchors"] if item["status"] == "resolved"] == [SLED]

    packet = _pick(learning_vault, "continue", MARIT)

    assert "learning" not in packet
    assert _misses(learning_vault) == {}
    working_set_runtime.reset_caches_for_tests()
    after = commands.op_activate_context(learning_vault, turn="continue")
    assert [item["path"] for item in after["anchors"] if item["status"] == "resolved"] == [MARIT]


def _every_file(*roots: Path) -> list[Path]:
    return [path for root in roots for path in root.rglob("*") if path.is_file()]


def test_no_turn_word_is_persisted_anywhere(learning_vault: Path) -> None:
    """Both sidecars, every log and ledger in the state root, and the vault."""
    word = "zorblatquux"
    packet = _pick(learning_vault, f"wie steht es um {word}", SLED)
    assert word in packet["learning"]["turn_terms"], "the caller hears its own words"
    _pick(learning_vault, f"wie steht es um {word}", SLED)
    commands.op_activate_context(learning_vault, turn=f"{word} continue")

    state_root = Path(os.environ["EXOMEM_STATE_ROOT"])
    files = _every_file(learning_vault, state_root)
    assert working_set_heat.sidecar_path(learning_vault) in files
    assert working_set_index.WorkingSetIndex(learning_vault).path in files
    for path in files:
        data = path.read_bytes()
        assert word.encode() not in data, path
        assert word.encode("utf-16-le") not in data, path


# --------------------------------------------------------------------------- #
# B5: the advisory
# --------------------------------------------------------------------------- #


def test_a_naming_miss_offers_one_bounded_advisory(learning_vault: Path) -> None:
    packet = _pick(learning_vault, "weiter mit dem Schlitten", SLED)

    advisory = packet["learning"]
    assert isinstance(advisory, dict)
    assert len(json.dumps(advisory, ensure_ascii=False)) <= working_set_learning.MAX_ADVISORY_CHARS
    assert working_set_learning.MAX_ADVISORY_CHARS == 900
    assert advisory["kind"] == "activation-naming"
    assert advisory["review"].startswith("exomem://review/activation-naming/")
    assert advisory["target"] == {"ref": SLED, "title": "Cargo Sled", "kind": "resource"}
    assert advisory["observed"]["misses"] == 1
    assert advisory["observed"]["turn_reached"] == "nothing"
    assert advisory["turn_terms"] == ["weiter", "schlitten"]
    assert [option["family"] for option in advisory["options"]] == ["name", "referential_cue"]
    assert "triage_memory" in advisory["rule"]


def test_the_advisory_carries_the_writers_guards(learning_vault: Path) -> None:
    advisory = _pick(learning_vault, NAMELESS, SLED)["learning"]
    name, cue = advisory["options"]

    page = (learning_vault / SLED).read_text(encoding="utf-8")
    assert name == {
        "family": "name",
        "tool": "edit_memory",
        "path": SLED,
        "field": "learned_aliases",
        "current": [],
        "expected_hash": content_hash(page),
    }
    assert cue == {
        "family": "referential_cue",
        "tool": "schema_memory",
        "subject": "activation-conventions",
        "operation": "save-conventions",
        "expected_hash": activation_conventions.load_conventions(learning_vault).content_hash,
    }


def test_repeated_misses_raise_the_count_and_a_dismissal_holds_until_two_more(
    learning_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _pick(learning_vault, NAMELESS, SLED)["learning"]
    second = _pick(learning_vault, NAMELESS, SLED)["learning"]
    assert (first["observed"]["misses"], second["observed"]["misses"]) == (1, 2)
    assert first["review"] == second["review"]
    # The bucket is `misses // 2`: the second miss moved it.
    assert first["fingerprint"] != second["fingerprint"]

    commands.op_triage_memory(
        learning_vault,
        ref=second["review"],
        action="dismiss",
        why="the user was testing",
        expected_fingerprint=second["fingerprint"],
    )
    _later(monkeypatch, 25)
    # Miss three is still in the dismissed bucket, past the cooldown.
    assert "learning" not in _pick(learning_vault, NAMELESS, SLED)
    # Miss four moves the bucket: new evidence, a new advisory.
    fourth = _pick(learning_vault, NAMELESS, SLED)["learning"]
    assert fourth["observed"]["misses"] == 4
    # Re-asked over two days: the advisory says so.
    assert fourth["observed"]["days"] == 2
    # The same target and bucket inside the 24-hour cooldown is not advised.
    assert "learning" not in _pick(learning_vault, NAMELESS, SLED)
    assert _misses(learning_vault) == {(SLED, "naming"): 5}


@pytest.mark.parametrize("silenced", ["quiet", "off", "proactive-capture-off"])
def test_quiet_family_and_proactive_capture_off_suppress_the_advisory(
    learning_vault: Path, monkeypatch: pytest.MonkeyPatch, silenced: str
) -> None:
    if silenced == "proactive-capture-off":
        monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: False)
    else:
        commands.op_triage_memory(
            learning_vault,
            ref=review_state.family_ref("activation-naming"),
            action=silenced,
            why="not now",
        )

    packet = _pick(learning_vault, NAMELESS, SLED)

    assert "learning" not in packet
    # The counter is evidence, not advice: it still counts.
    assert _misses(learning_vault) == {(SLED, "naming"): 1}


def test_the_advisory_is_never_cached_and_not_in_the_key(learning_vault: Path) -> None:
    assert "learning" not in inspect.signature(working_set_runtime.cache_key).parameters
    first = _pick(learning_vault, NAMELESS, SLED)
    second = _pick(learning_vault, NAMELESS, SLED)

    assert first["learning"]["observed"]["misses"] == 1
    assert second["learning"]["observed"]["misses"] == 2
    with working_set_runtime._CACHE_LOCK:
        cached = list(working_set_runtime._PACKET_CACHE.values())
    assert cached, "the packet itself must be cached, or this proves nothing"
    assert all("learning" not in packet for packet in cached)


def test_activation_never_stamps_the_review_state(learning_vault: Path) -> None:
    path = review_state.state_path(learning_vault)
    assert _pick(learning_vault, NAMELESS, SLED)["learning"]
    assert not path.exists()

    commands.op_triage_memory(
        learning_vault,
        ref=review_state.family_ref("near-duplicate"),
        action="quiet",
        why="unrelated",
    )
    before = path.read_bytes()
    assert _pick(learning_vault, NAMELESS, SLED)["learning"]
    assert path.read_bytes() == before


def _vault_bytes(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }


def test_learning_writes_nothing_by_itself(learning_vault: Path) -> None:
    before = _vault_bytes(learning_vault)
    packet = _pick(learning_vault, NAMELESS, SLED)
    assert packet["learning"]
    assert _vault_bytes(learning_vault) == before
    assert activation_conventions.override_path(learning_vault).exists() is False


def test_a_stale_advisory_hash_refuses_the_write(learning_vault: Path) -> None:
    name, cue = _pick(learning_vault, NAMELESS, SLED)["learning"]["options"]
    _work(learning_vault, SLED, value="2026-09-21")

    with pytest.raises(Exception, match="expected_hash"):
        writer_lease.invoke_command(
            _command("edit_memory"),
            learning_vault,
            path=name["path"],
            why="the user calls it this",
            operation={
                "kind": "patch_frontmatter",
                "field": name["field"],
                "value": ["Schlitten"],
                "expected_hash": name["expected_hash"],
            },
        )

    concurrent = activation_conventions.load_conventions(learning_vault).content_hash
    commands.op_schema_memory(
        learning_vault,
        subject="activation-conventions",
        operation="save-conventions",
        proposal={"schema_version": 1, "referential": {"add_filler": ["bitte"]}},
        why="another agent's change",
        expected_hash=concurrent,
    )
    with pytest.raises(ValueError, match="STALE_ACTIVATION_CONVENTIONS_REGISTRY"):
        commands.op_schema_memory(
            learning_vault,
            subject=cue["subject"],
            operation=cue["operation"],
            proposal={"schema_version": 1, "referential": {"add_cues": ["weiter"]}},
            why="the user resumes in German",
            expected_hash=cue["expected_hash"],
        )


def test_triage_memory_resolves_an_activation_naming_ref(learning_vault: Path) -> None:
    advisory = _pick(learning_vault, NAMELESS, SLED)["learning"]

    snoozed = commands.op_triage_memory(
        learning_vault,
        ref=advisory["review"],
        action="snooze",
        until="2099-01-01",
        expected_fingerprint=advisory["fingerprint"],
    )
    assert snoozed["ref"] == advisory["review"]
    assert snoozed["state"] == "snoozed"

    dismissed = commands.op_triage_memory(
        learning_vault,
        ref=advisory["review"],
        action="dismiss",
        why="not a name",
        expected_fingerprint=advisory["fingerprint"],
    )
    assert dismissed["state"] == "dismissed"
    assert "activation-naming" in review_state.registered_families()
    with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
        commands.op_triage_memory(
            learning_vault, ref=advisory["review"], action="dismiss",
            expected_fingerprint=advisory["fingerprint"],
        )
