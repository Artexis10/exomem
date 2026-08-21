"""`bootstrap(profile="compact")` must actually be compact.

The profile existed but did almost nothing: compact was 64,070 bytes and full was
65,039 — a 1.5% saving — so every generic-MCP session start spent roughly 16,000
tokens of the caller's context before any work happened. The largest single cause was
shipping all six built-in packs' `agent_instructions` when only the *selected* pack's
guidance can ever apply.

These tests pin the saving so the profile cannot quietly collapse back into full.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import warnings

import pytest

from exomem import commands

#: Ceiling for the compact payload. Chosen above the measured floor so ordinary growth
#: is fine, and far below the 64 KB regression point. Lower it when compact shrinks
#: further; never raise it without deciding the extra bytes earn a caller's context.
#:
#: Raised once, from 56,000, and the decision is on the record with its arithmetic.
#: The epistemic contract added 3,198 bytes to a 52,877-byte floor, taking compact to
#: 56,075 — 75 bytes past the old ceiling. Those bytes earn their place, because the
#: payload is the entire contract a hosted or generic MCP client ever receives, and
#: without them such a client never learns that raw material is append-only, that a
#: changed conclusion is superseded rather than overwritten, or that a refuted claim
#: stays active. That doctrine reached only skill-capable Claude surfaces, so one vault
#: got two epistemologies depending on which client wrote to it.
#:
#: Fitting under 56,000 was possible and was declined on the merits: dropping the
#: `kinds` (193 B) and `relations` (145 B) sub-blocks of the payload's epistemic
#: vocabulary would have landed compact at 55,737, a real 263 bytes clear. They restate
#: material the payload carries elsewhere, and they were kept anyway, because an agent
#: reading the doctrine should not have to assemble the vocabulary from three other
#: sections to act on it.
#:
#: Be clear about what the raise is: this change spent the entire growth budget the old
#: ceiling expressed and pre-authorised 1,925 bytes more. It is not headroom restored.
#: A second addition of this size must argue for itself from scratch, and 58,000 still
#: sits ~6 KB below the 64,070-byte regression point the gate was built to catch.
#: `MINIMUM_SAVING_RATIO` below is untouched; the saving moved 32.74% -> 31.46%.
#:
#: Raised a second time, and the arithmetic is again on the record — re-measured
#: on the final tree rather than carried over from a draft. The measured floor at
#: 58,000 was 57,872 — 128 bytes of headroom, i.e. none. The due-state carriers add
#: three `authoring_contract.post_write` entries (`due_state`,
#: `due_state_handling`, `due_state_authority`) totalling 1,224 bytes, taking
#: compact to 59,096. The block those entries describe costs this measurement
#: nothing: it is vault-derived and absent on the empty fixture, and it is bounded
#: at five references anyway.
#:
#: Why the bytes earn their place. This payload is the ENTIRE contract a hookless
#: client receives, and the due-state block is a channel that arrives unasked on
#: ordinary results. An agent that receives counts it was never taught to read has
#: two failure modes and both are worse than the bytes: ignore them, and the change
#: delivers nothing; act on every one, and the substrate becomes the nag its own
#: design refuses to be. The three entries are the smallest statement of what the
#: counts are, when to raise one, and that the runtime never acts on them.
#:
#: Fitting under 58,000 was examined and rejected as dishonest rather than tight.
#: The only reductions available were dropping `due_state_authority` (183 B, the
#: line that says the runtime never resolves or archives anything on the counts'
#: behalf) or collapsing the handling guidance to its first clause (~250 B, losing
#: the fingerprint rule and the silence-beats-bureaucracy rule). Both remove
#: exactly the restraint the counters need in order not to become a nuisance.
#:
#: The new ceiling is 59,500: 404 bytes of headroom, deliberately less than the
#: last raise pre-authorised, and still ~4.5 KB below the 64,070-byte regression
#: point. `MINIMUM_SAVING_RATIO` is untouched, and the saving moved DOWN — 35.46%
#: without these entries to 34.98% with them — because bytes added to compact make
#: compact resemble full a little more. It stays far above the 15% floor. (The
#: 31.46% recorded for the previous raise is stale: the payload has moved since,
#: and the two figures here are both measured on the current tree, which is why
#: they do not chain onto it.)
COMPACT_BYTE_CEILING = 59_500

#: The defect was compact and full being near-identical. A profile that does not
#: measurably differ from full is not a profile.
MINIMUM_SAVING_RATIO = 0.15


@pytest.fixture(scope="module")
def payloads() -> dict[str, dict]:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    return {
        profile: commands.op_bootstrap(root, profile=profile)
        for profile in ("compact", "full", "diagnostics")
    }


def _size(payload: dict) -> int:
    return len(json.dumps(payload))


#: Headroom below which the ceiling stops being a budget and becomes a cliff.
#: Warn rather than fail: the remaining bytes are still legitimately spendable,
#: and turning "nearly full" into a failure would just be the ceiling moved down
#: without the argument the ceiling's own docstring demands.
HEADROOM_WARNING_BYTES = 512


def test_compact_stays_under_its_byte_ceiling(payloads):
    size = _size(payloads["compact"])
    assert size <= COMPACT_BYTE_CEILING, (
        f"compact bootstrap is {size:,} bytes (~{size // 4:,} tokens), over the "
        f"{COMPACT_BYTE_CEILING:,} ceiling by {size - COMPACT_BYTE_CEILING:,}"
    )
    headroom = COMPACT_BYTE_CEILING - size
    if headroom < HEADROOM_WARNING_BYTES:
        # The failure mode this catches is not the ceiling being wrong, it is
        # the ceiling being reached *silently*. Compact grew to 70 bytes of
        # headroom and nobody knew until an unrelated release PR went red two
        # merges later, which is a bad place to first read the argument for why
        # the number is what it is.
        warnings.warn(
            f"compact bootstrap is {size:,} bytes with only {headroom:,} bytes "
            f"under the {COMPACT_BYTE_CEILING:,} ceiling. The next addition of "
            "any size will trip it. Either trim compact, or raise the ceiling "
            "with the reasoning the constant's docstring requires -- but decide "
            "it deliberately rather than discovering it as a red CI run.",
            stacklevel=2,
        )


def test_compact_is_materially_smaller_than_full(payloads):
    compact, full = _size(payloads["compact"]), _size(payloads["full"])
    saving = (full - compact) / full
    assert saving >= MINIMUM_SAVING_RATIO, (
        f"compact saves only {saving:.1%} over full ({compact:,} vs {full:,}); the "
        "profile has collapsed back into full"
    )


def test_profiles_are_ordered_by_size(payloads):
    assert _size(payloads["compact"]) < _size(payloads["full"]) <= _size(
        payloads["diagnostics"]
    )


# ------------------------------------------------------------------ what was trimmed


def test_compact_omits_unselected_pack_guidance(payloads):
    """Only the selected pack's instructions can apply; the rest are dead weight."""
    available = payloads["compact"]["knowledge_packs"]["available"]
    assert available, "the catalogue must still be discoverable"
    for pack in available:
        assert "agent_instructions" not in pack
        assert "examples" not in pack


def test_compact_still_names_every_pack(payloads):
    """Trimming bodies must not hide which packs exist."""
    compact_ids = {p["id"] for p in payloads["compact"]["knowledge_packs"]["available"]}
    full_ids = {p["id"] for p in payloads["full"]["knowledge_packs"]["available"]}
    assert compact_ids == full_ids
    for pack in payloads["compact"]["knowledge_packs"]["available"]:
        assert pack["name"]


def test_full_retains_the_complete_catalogue(payloads):
    assert any(
        "agent_instructions" in pack
        for pack in payloads["full"]["knowledge_packs"]["available"]
    )


# --------------------------------------------------------------- what must survive


def test_selected_pack_guidance_survives_in_compact(payloads):
    """The one pack whose instructions actually apply must keep them."""
    selected = json.dumps(payloads["compact"]["knowledge_packs"]["selected"])
    assert "agent_instructions" in selected


def test_compact_action_catalogues_reference_selected_pack_guidance_once(payloads):
    """Action aliases point at the selected pack; they do not repeat its body."""
    compact = payloads["compact"]
    for catalogue_name in ("simple_actions", "front_door_actions"):
        for action in compact[catalogue_name].values():
            for guidance in action.get("selected_pack_guidance", []):
                assert set(guidance) <= {"pack_id", "name"}

    assert any(
        "agent_instructions" in guidance
        for action in payloads["full"]["simple_actions"].values()
        for guidance in action.get("selected_pack_guidance", [])
    )


def test_compact_still_teaches_the_core_loop(payloads):
    """A smaller contract is only a win if it is still a contract."""
    compact = payloads["compact"]
    workflow = compact["workflow"]
    assert workflow["save_rule"]
    assert workflow["miss_rule"]
    for section in ("server", "active_capabilities", "governance", "search_guidance"):
        assert section in compact, section


def test_bootstrap_planning_contract_is_complete_and_exact(payloads):
    planning = payloads["full"]["planning"]

    assert planning["route"] == {
        "tool": "plan_memory",
        "actions": ["inspect", "create", "query", "add", "update", "triage"],
    }
    assert planning["kinds"] == ["area", "outcome", "initiative", "work-item"]
    assert planning["horizons"] == ["inbox", "week", "month", "quarter", "year", "multi-year"]
    assert planning["lifecycle"] == ["active", "archived"]
    assert planning["priorities"] == ["critical", "high", "medium", "low", "none"]
    assert planning["commitments"] == ["uncommitted", "considering", "committed"]
    for key in (
        "default_capture",
        "manual_first",
        "template_independence",
        "horizon_semantics",
        "intent_first_routing",
        "evidence_execution_boundary",
        "execution_truth_boundary",
    ):
        assert planning[key]


def test_compact_and_full_agree_on_everything_but_detail(payloads):
    """The trim is a presentation choice; it must not change what is advertised."""
    compact, full = payloads["compact"], payloads["full"]
    assert set(compact) <= set(full)
    assert compact["server"] == full["server"]
    assert compact["active_capabilities"] == full["active_capabilities"]
