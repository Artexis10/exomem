"""The live capture policy rides the activation packet.

Every case drives the real activation path under a real request scope. The saved preference and
the envelope override are set through the product's own writers, and the disposition is compared
with what bootstrap serves for the same caller.
"""

from __future__ import annotations

import asyncio

import pytest
from test_connector_boundary import configured_boundary as configured_boundary

from exomem import capture_carrier, commands, envelope, prominence, working_set_runtime, writer_lease
from exomem.governance.principal import RequestPrincipal, request_scope

TURN = "what do we know about the tidal ledger?"
CALLER = RequestPrincipal("principal:person-a", surface="mcp")


@pytest.fixture
def served(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    for key in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL"):
        monkeypatch.delenv(key, raising=False)
    yield vault


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
        later = _activate(served, continuity=small["continuity"])

    assert "capture_policy" not in small
    assert "capture_policy" in later


def test_an_attached_block_is_counted_in_the_packet_budget() -> None:
    block = {"disposition": "silent", "say": "x" * 100}
    packet = {"budget": {"limit_chars": 4000, "used_chars": 1000}}

    capture_carrier.for_packet(packet, capture_carrier.Policy(block, "d", False, capture_carrier.size(block)))

    assert packet["capture_policy"] == block
    assert packet["budget"]["used_chars"] == 1000 + capture_carrier.size(block)


def test_a_block_that_would_overflow_the_packet_is_left_off_and_uncounted() -> None:
    block = {"disposition": "silent", "say": "x" * 100}
    packet = {"budget": {"limit_chars": 1000, "used_chars": 990}}

    capture_carrier.for_packet(packet, capture_carrier.Policy(block, "d", False, capture_carrier.size(block)))

    assert "capture_policy" not in packet
    assert packet["budget"]["used_chars"] == 990


def test_stamping_a_token_keeps_what_the_token_already_says() -> None:
    """The digest joins the signed thread and refs; it does not replace or invalidate them."""
    token = working_set_runtime.encode_continuity(
        identity="index-a", roles_hash="r", conventions_hash="c", generation=3, refs=["a.md"], roles=["anchor"],
        minted_ns=10, thread="thread-a", thread_ns=5, salt="salt",
    )

    stamped = working_set_runtime.with_capture_policy(token, "digest-a")

    assert working_set_runtime.decode_continuity(stamped) == {
        **working_set_runtime.decode_continuity(token),
        "capture_policy": "digest-a",
    }
    assert working_set_runtime.decode_continuity(token).get("mac")


def test_stateless_http_sends_the_block_once_per_token_and_again_after_a_change(
    configured_boundary, served, monkeypatch
) -> None:
    """Over the shipped transport every request is a fresh session, so only the echoed token can say what was sent."""
    from starlette.testclient import TestClient
    from test_connector_boundary import ISSUER

    from exomem import server, server_auth, state_migration
    from exomem.auth_sessions import SessionIdentity

    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    for name, value in {
        "GITHUB_CLIENT_ID": "fixture-client",
        "GITHUB_CLIENT_SECRET": "fixture-secret",
        "EXOMEM_GITHUB_USERNAME": "fixture-owner",
        "EXOMEM_JWT_SIGNING_KEY": "temporary-boundary-signing-root",
        "EXOMEM_UPLOAD_TOKEN": "temporary-boundary-transfer-secret",
        "EXOMEM_VAULT_PATH": str(served),
    }.items():
        monkeypatch.setenv(name, value)
    authority = server_auth.build_session_authority(base_url=ISSUER)
    bearer, _record = asyncio.run(
        authority.issue(
            client_id="full", scopes=["exomem:read", "exomem:write"],
            identity=SessionIdentity(github_user_id=4242, github_login="fixture-owner"),
        )
    )
    state_migration.arm_connector_boundary_offline(
        served, authority=state_migration.assert_offline_migration_authority(source="capture carrier test")
    )

    def call(client, name: str, arguments: dict) -> dict:
        reply = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {bearer}", "Accept": "application/json",
                "mcp-protocol-version": "2025-06-18", "mcp-method": "tools/call", "mcp-name": name,
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": arguments,
                "_meta": {"io.modelcontextprotocol/protocolVersion": "2025-06-18",
                          "io.modelcontextprotocol/clientCapabilities": {},
                          "io.modelcontextprotocol/clientInfo": {"name": "fixture", "version": "1"}},
            }},
        )
        result = reply.json()["result"]
        assert not result.get("isError"), reply.text[:500]
        return result["structuredContent"]

    with TestClient(server.build_server(require_auth=True).http_app(stateless_http=True, json_response=True)) as client:
        token, delivered = None, []
        for _ in range(4):
            packet = call(client, "recall_context", {"turn": TURN, **({"continuity": token} if token else {})})
            token = packet["continuity"]
            delivered.append("capture_policy" in packet)
        before = call(client, "configure_memory", {})
        call(client, "configure_memory", {"action": "set", "prominence": "light", "expected_revision": before["revision"]})
        changed = call(client, "recall_context", {"turn": TURN, "continuity": token})

    assert delivered == [True, False, False, False]
    assert changed["capture_policy"]["disposition"] == "off"


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
