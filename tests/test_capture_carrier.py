"""The live capture policy rides the activation packet.

Every case drives the real activation path under a real request scope. The saved preference and
the envelope override are set through the product's own writers, and the disposition is compared
with what bootstrap serves for the same caller.
"""

from __future__ import annotations

import pytest

from exomem import capture_carrier, commands, envelope, prominence, writer_lease
from exomem import due_state as due_state_module
from exomem.governance.principal import RequestPrincipal, request_scope

TURN = "what do we know about the tidal ledger?"
CALLER = RequestPrincipal("principal:person-a", surface="mcp")


@pytest.fixture
def served(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    for key in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL"):
        monkeypatch.delenv(key, raising=False)
    due_state_module.reset_emission_state()
    yield vault
    due_state_module.reset_emission_state()


def _invoke(name: str, vault, **kwargs):
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
    return writer_lease.invoke_command(command, vault, **kwargs)


def _activate(vault, **kwargs) -> dict:
    return _invoke("activate_context", vault, turn=TURN, session="conversation-a", **kwargs)


def _bootstrap_disposition(vault) -> str:
    served = _invoke("bootstrap", vault, profile="compact")
    return served["engagement"]["envelope"]["classes"]["proactive_capture"]["disposition"]


@pytest.mark.parametrize("disposition", envelope.RANGES["proactive_capture"])
def test_the_block_states_the_disposition_that_bootstrap_serves(served, disposition) -> None:
    envelope.set_disposition("proactive_capture", disposition)

    with request_scope(CALLER):
        block = _activate(served)["capture_policy"]
        bootstrapped = _bootstrap_disposition(served)

    assert block["disposition"] == bootstrapped == disposition
    assert block["say"].startswith(f"proactive_capture is {disposition}:")
    assert (disposition == "off") == ("Capture stable preferences" not in block["say"])


def test_the_block_withholds_what_an_unreadable_preference_withholds(served, monkeypatch) -> None:
    from exomem import prominence_preferences
    from exomem.cli_ops import OpError

    def unreadable(*args, **kwargs):
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")

    with request_scope(CALLER):
        monkeypatch.setattr(prominence_preferences, "inspect", unreadable)
        block = _activate(served)["capture_policy"]
        bootstrapped = _bootstrap_disposition(served)

    assert block["disposition"] == bootstrapped == "off"


def test_the_block_is_sent_once_and_sent_again_after_the_token_is_dropped(served) -> None:
    with request_scope(CALLER):
        first = _activate(served)
        held = _activate(served, continuity=first["continuity"])
        compacted = _activate(served)

    assert "capture_policy" in first
    assert "capture_policy" not in held
    assert compacted["capture_policy"] == first["capture_policy"]


def test_a_saved_preference_reaches_the_next_turn_that_holds_a_token(served) -> None:
    with request_scope(CALLER):
        first = _activate(served)
        before = _invoke("configure_memory", served)
        _invoke(
            "configure_memory", served, action="set", prominence="light",
            expected_revision=before["revision"],
        )
        changed = _activate(served, continuity=first["continuity"])

    assert first["capture_policy"]["disposition"] == "silent"
    assert changed["capture_policy"]["disposition"] == "off"


def test_a_full_map_still_delivers_the_block_inside_limit_chars(served) -> None:
    with request_scope(CALLER):
        packet = _activate(served, max_chars=2000)

    assert "capture_policy" in packet
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"] == 2000


def test_a_request_too_small_for_the_block_is_not_starved_and_is_owed_it_later(served) -> None:
    with request_scope(CALLER):
        small = _activate(served, max_chars=800)
        later = _activate(served)

    assert "capture_policy" not in small
    assert "capture_policy" in later


def test_an_attached_block_is_counted_in_the_packet_budget(served) -> None:
    block = {"disposition": "silent", "say": "x" * 100}
    packet = {"budget": {"limit_chars": 4000, "used_chars": 1000}}

    capture_carrier.for_packet(served, packet, capture_carrier.Owed(block, "d"), session="s")

    assert packet["capture_policy"] == block
    assert packet["budget"]["used_chars"] == 1000 + capture_carrier.size(block)


def test_a_block_that_would_overflow_the_packet_is_left_off_and_uncounted(served) -> None:
    packet = {"budget": {"limit_chars": 1000, "used_chars": 990}}

    capture_carrier.for_packet(
        served, packet, capture_carrier.Owed({"disposition": "silent", "say": "x" * 100}, "d"), session="s"
    )

    assert "capture_policy" not in packet
    assert packet["budget"]["used_chars"] == 990


def test_the_worst_case_block_is_ascii_and_fits_the_rendered_ceiling(served) -> None:
    """Silent carries the longest text. The hook renders it under its header; one block, 800 bytes."""
    from test_retrieve_nudge_working_set import hook

    rendered = hook._capture_policy_text(
        {"disposition": "silent", "say": prominence.capture_policy_text("silent")}
    )

    assert rendered.isascii()
    assert 0 < len(rendered.encode("ascii")) <= 800


def test_every_disposition_the_envelope_allows_has_a_meaning() -> None:
    """A new range value without a sentence would silently disable the carrier, which never raises."""
    assert set(envelope.PROACTIVE_CAPTURE_MEANING) == set(envelope.RANGES["proactive_capture"])
