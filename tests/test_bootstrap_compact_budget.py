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

import pytest
from budget_gate import check_budget

from exomem import commands

#: Ceiling for the compact payload, which since `shrink-bootstrap` is the always-served
#: CORE (reference detail is served on demand through `bootstrap(section=...)`).
#:
#: Ruled at 15,000 bytes at `maximal` on the worst-case surface. It replaces a ceiling of
#: 63,300 that had been raised five times to fit whatever had been added. The core was
#: measured at 14,407 bytes (claude-code, maximal) when this was set: about 590 bytes of
#: margin, deliberately above the 512-byte warning band below. The old history is not
#: reproduced here; the arithmetic that matters is `openspec/changes/shrink-bootstrap`.
#:
#: Do not raise it to fit a new block. A block earns core bytes only by being a rule that
#: prevents a known incident, and the way to argue that is a new entry in `CORE_RULES` in
#: `tests/test_bootstrap_core.py`. Reference detail goes in a section, which has its own
#: ceiling there. `tests/test_bootstrap_core.py` also asserts the per-level, per-surface
#: ceiling; this module keeps the original headroom and profile-ratio checks.
COMPACT_BYTE_CEILING = 15_000

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
    } | {
        # The complete pre-core compact payload, for the tests below that pin what
        # compact still teaches; `payloads["compact"]` is the always-served core.
        "reference": commands.op_bootstrap(root, profile="compact", section="all")
    }


def _size(payload: dict) -> int:
    return len(json.dumps(payload))


#: Headroom below which the ceiling stops being a budget and becomes a cliff.
#: Warn rather than fail: the remaining bytes are still legitimately spendable,
#: and turning "nearly full" into a failure would just be the ceiling moved down
#: without the argument the ceiling's own docstring demands.
#: 400 -> 512 (2026-09-25): the stopgap drop for `episode_memory` is repaid. The
#: compact-bootstrap trim recorded on `COMPACT_BYTE_CEILING` above recovered 217
#: bytes of redundant prose, restoring the default level to 664/655 bytes of
#: headroom `(default surface, claude-code)` -- clear of the 512-byte band this
#: constant re-asserts, and of the 256-byte floor `maximal` is held to below.
HEADROOM_WARNING_BYTES = 512


def test_compact_stays_under_its_byte_ceiling(payloads):
    """Fails above the ceiling; warns (never fails) inside the headroom band."""
    check_budget(
        _size(payloads["compact"]),
        ceiling=COMPACT_BYTE_CEILING,
        band=HEADROOM_WARNING_BYTES,
        label="compact bootstrap",
    )


def test_a_hook_capable_client_still_clears_the_ceiling(monkeypatch):
    """A hook-capable client at the DEFAULT level, which is what this measures.

    The previous version of this docstring claimed `engagement.hook_cadence`
    rides on the coding context while `maximal` rides on the conversational one,
    so "neither surface is the worst case for the other". That is wrong, and it
    hid the real worst case: `hook_cadence` is served at every level, `maximal`
    included, so the hook-capable surface and the longest contract prose combine.
    Measured 2026-09-18: `maximal` on `claude-code` is 63,117 bytes, 183 under
    the ceiling, against 62,752 (548 under) at the default level here.

    So this test is not the worst case and does not claim to be. It covers the
    level a real install without a stored preference resolves through, and keeps
    the warning margin for that one.
    `tests/test_bootstrap_activation_carrier.py` carries the full
    level-by-surface matrix, where the hard ceiling is asserted everywhere and
    the margin only at the default level.
    """
    monkeypatch.setenv("EXOMEM_SURFACE", "claude-code")
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    payload = commands.op_bootstrap(root, profile="compact")
    size = _size(payload)

    assert "hook_cadence" in payload["engagement"]
    check_budget(
        size,
        ceiling=COMPACT_BYTE_CEILING,
        band=HEADROOM_WARNING_BYTES,
        label="compact bootstrap for a hook-capable client",
    )


def test_compact_is_materially_smaller_than_full(payloads):
    compact, full = _size(payloads["compact"]), _size(payloads["full"])
    saving = (full - compact) / full
    assert saving >= MINIMUM_SAVING_RATIO, (
        f"compact saves only {saving:.1%} over full ({compact:,} vs {full:,}); the "
        "profile has collapsed back into full"
    )


# ------------------------------------------------------------------ what was trimmed


def test_compact_omits_unselected_pack_guidance(payloads):
    """Only the selected pack's instructions can apply; the rest are dead weight."""
    available = payloads["reference"]["knowledge_packs"]["available"]
    assert available, "the catalogue must still be discoverable"
    for pack in available:
        assert "agent_instructions" not in pack
        assert "examples" not in pack


def test_compact_still_names_every_pack(payloads):
    """Trimming bodies must not hide which packs exist."""
    compact_ids = {p["id"] for p in payloads["reference"]["knowledge_packs"]["available"]}
    full_ids = {p["id"] for p in payloads["full"]["knowledge_packs"]["available"]}
    assert compact_ids == full_ids
    for pack in payloads["reference"]["knowledge_packs"]["available"]:
        assert pack["name"]


def test_full_retains_the_complete_catalogue(payloads):
    assert any(
        "agent_instructions" in pack
        for pack in payloads["full"]["knowledge_packs"]["available"]
    )


# --------------------------------------------------------------- what must survive


def test_selected_pack_guidance_survives_in_compact(payloads):
    """The one pack whose instructions actually apply must keep them."""
    selected = json.dumps(payloads["reference"]["knowledge_packs"]["selected"])
    assert "agent_instructions" in selected


def test_compact_action_catalogues_reference_selected_pack_guidance_once(payloads):
    """Action aliases point at the selected pack; they do not repeat its body."""
    compact = payloads["reference"]
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
    compact = payloads["reference"]
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
    compact, full = payloads["reference"], payloads["full"]
    assert set(compact) <= set(full)
    assert compact["server"] == full["server"]
    assert compact["active_capabilities"] == full["active_capabilities"]
