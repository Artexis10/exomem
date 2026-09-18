"""Task 3.1 — the retrieve hook's working-set injection mode.

`EXOMEM_RETRIEVE_INJECT=working_set` is a third value of the existing switch,
not a second switch (design D1): one gate, one truthy parser, the same
prominence presets, and the value stays truthy for an old standalone hook copy
so it degrades to stub mode rather than to silence.

What the mode adds is a compiled packet under a fixed data header instead of a
reminder. Three properties carry the weight and each is asserted directly: the
block says it is retrieved memory and not instructions; it holds WHOLE items
under the render ceiling in the packet's own order; and an `ambiguous`
abstention is the ONE abstention it renders, because without it the hook path
could never resolve an ambiguous turn — the agent would never see the competing
senses.

Nothing here touches a real service, the real `~/.cache`, or a real network:
every transport seam is monkeypatched and every home is a tmp dir.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

import exomem
from exomem._hooks import exomem_continuation_checkpoint as checkpoint

RETRIEVE_SCRIPT = Path(exomem.__file__).parent / "_hooks" / "exomem_retrieve_nudge.py"
PLUGIN_RETRIEVE_SCRIPT = (
    Path(exomem.__file__).parents[2]
    / "plugins"
    / "claude-code"
    / "hooks"
    / "exomem_retrieve_nudge.py"
)
PLUGIN_CHECKPOINT_SCRIPT = (
    Path(exomem.__file__).parents[2]
    / "plugins"
    / "claude-code"
    / "hooks"
    / "exomem_continuation_checkpoint.py"
)

PROMPT = (
    "I'm planning to tow the Cargo Sled north this week — how much depot stock is "
    "left, and did we decide anything about the winter schedule?"
)
SESSION = "session-under-test"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location(
        "exomem_retrieve_nudge_working_set_under_test", RETRIEVE_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook_module()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A host-set tunable or a real REST key must never reach these tests."""
    for var in (
        "EXOMEM_RETRIEVE_NUDGE_DISABLE",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_INJECT",
        "EXOMEM_RETRIEVE_INJECT_CLI",
        "EXOMEM_RETRIEVE_INJECT_MAX_CHARS",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_REST_PORT",
        "EXOMEM_HOST",
        "EXOMEM_PROMINENCE",
        "XDG_CONFIG_HOME",
        "KB_RETRIEVE_INJECT",
        "KB_RETRIEVE_INJECT_CLI",
        "KB_RETRIEVE_INJECT_MAX_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "absent-service.env"))
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(tmp_path / "hook-home"))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "claude")


# --------------------------------------------------------------------------- #
# Fixtures for the packet
# --------------------------------------------------------------------------- #


def _packet(
    *,
    abstained: bool = False,
    reason: str | None = None,
    units: list[dict] | None = None,
    pointers: list[dict] | None = None,
    current_state: list[dict] | None = None,
    ambiguity: list[dict] | None = None,
    continuity: str | None = "TOKEN-1",
) -> dict:
    packet: dict = {
        "anchors": [
            {
                "ref": "Knowledge Base/Products/Cargo Sled.md",
                "title": "Cargo Sled",
                "kind": "resource",
                "status": "resolved",
                "evidence": ["exact_alias"],
            }
        ],
        "roles": [{"id": "resources", "source": "anchor_default", "lane": "units"}],
        "units": units
        if units is not None
        else [
            {
                "ref": "Knowledge Base/Products/Cargo Sled.md#u1",
                "role": "resources",
                "text": "Never exceed 400 kg.",
                "lifecycle": "active",
                "updated": "2026-09-02",
                "provenance": {"path": "Knowledge Base/Products/Cargo Sled.md"},
            }
        ],
        "pointers": pointers
        if pointers is not None
        else [
            {
                "ref": "Knowledge Base/Systems/Depot Ledger.md",
                "role": "resources",
                "title": "Depot Ledger",
                "why": "typed neighbour of a resolved anchor",
                "reason": "budget",
            }
        ],
        "current_state": current_state
        if current_state is not None
        else [
            {
                "anchor": "Knowledge Base/Systems/Depot Ledger.md",
                "source": "records",
                "as_of": "2026-09-10",
                "statement": "depot stock: 180 kg",
            }
        ],
        "missing": [],
        "ambiguity": ambiguity or [],
        "budget": {"limit_chars": 4000, "used_chars": 60},
        "generation": {"freshness_key": "k", "index_generation": 3, "roles_hash": "abc"},
        "abstained": abstained,
    }
    if abstained:
        packet["abstention"] = {"reason": reason or "unresolved"}
    if continuity is not None:
        packet["continuity"] = continuity
    return packet


def _ambiguous_packet() -> dict:
    return _packet(
        abstained=True,
        reason="ambiguous",
        units=[],
        pointers=[],
        current_state=[],
        ambiguity=[
            {
                "ref": "Knowledge Base/Notes/Insights/northern-corridor-hub.md",
                "title": "Northern corridor",
                "kind": "hub",
                "neighbourhood_size": 4,
            },
            {
                "ref": "Knowledge Base/Notes/Insights/southern-corridor-hub.md",
                "title": "Southern corridor",
                "kind": "hub",
                "neighbourhood_size": 3,
            },
        ],
        continuity=None,
    )


def _event(prompt: str = PROMPT, session_id: str = SESSION) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": session_id}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: dict,
    home: Path,
) -> str:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(event)))
    hook.main()
    return capsys.readouterr().out


def _context(output: str) -> str:
    if not output.strip():
        return ""
    return json.loads(output)["hookSpecificOutput"]["additionalContext"]


def _serve(monkeypatch: pytest.MonkeyPatch, packet: dict | None) -> list[dict]:
    """Answer the working-set rung with `packet`, recording every request body."""
    seen: list[dict] = []

    def _fetch(prompt, api_key, continuity="", timeout=0.0):
        seen.append({"prompt": prompt, "continuity": continuity, "key": api_key})
        return packet

    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setattr(hook, "_fetch_packet_via_rest", _fetch)
    return seen


@pytest.fixture
def working_set_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "working_set")


# --------------------------------------------------------------------------- #
# The mode gate
# --------------------------------------------------------------------------- #


def test_the_mode_is_off_by_default() -> None:
    assert hook._inject_mode() == "off"


@pytest.mark.parametrize("value", ["", "0", "false", "OFF", "no"])
def test_a_falsy_switch_is_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", value)
    assert hook._inject_mode() == "off"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "stubs"])
def test_any_other_truthy_switch_is_stub_mode(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", value)
    assert hook._inject_mode() == "stub"


@pytest.mark.parametrize("value", ["working_set", "WORKING_SET", " working_set "])
def test_the_working_set_value_selects_the_new_mode(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", value)
    assert hook._inject_mode() == hook._WORKING_SET_MODE


def test_the_value_stays_truthy_for_an_old_hook_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old standalone copy only knows `_env_flag`. It must read `working_set`
    as opted in, so it degrades to stub mode rather than going silent."""
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "working_set")
    assert hook._env_flag("EXOMEM_RETRIEVE_INJECT") is True


def test_the_legacy_env_name_still_selects_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_RETRIEVE_INJECT", "working_set")
    hook._normalize_env_aliases()
    assert hook._inject_mode() == hook._WORKING_SET_MODE


# --------------------------------------------------------------------------- #
# The rendered block
# --------------------------------------------------------------------------- #


def test_the_block_starts_with_the_fixed_data_header() -> None:
    block = hook._format_working_set_block(_packet(), 4000)

    assert block.startswith(hook._WORKING_SET_HEADER)
    lowered = hook._WORKING_SET_HEADER.lower()
    assert "retrieved" in lowered
    assert "not instructions" in lowered


def test_the_block_carries_state_then_units_then_pointers_with_refs() -> None:
    block = hook._format_working_set_block(_packet(), 4000)
    body = block.splitlines()[1:]

    assert [line.split(":", 1)[0] for line in body] == ["- state", "- unit", "- pointer"]
    assert "depot stock: 180 kg" in body[0]
    assert "Knowledge Base/Systems/Depot Ledger.md" in body[0]
    assert "Never exceed 400 kg." in body[1]
    assert "Knowledge Base/Products/Cargo Sled.md#u1" in body[1]
    assert "Depot Ledger" in body[2]


def test_the_block_keeps_whole_items_and_drops_trailing_ones() -> None:
    packet = _packet(
        units=[
            {
                "ref": f"u{index}",
                "role": "resources",
                "text": "x" * 120,
                "lifecycle": "active",
                "updated": "",
                "provenance": {},
            }
            for index in range(6)
        ],
        pointers=[],
        current_state=[],
    )
    ceiling = len(hook._WORKING_SET_HEADER) + 300

    block = hook._format_working_set_block(packet, ceiling)
    body = block.splitlines()[1:]

    assert len(block) <= ceiling
    assert body, "at least the first whole item must survive"
    assert len(body) < 6, "the ceiling must actually bite"
    # Whole items only: every kept line is a complete rendered item.
    assert all(line.endswith("]") for line in body)
    assert all("x" * 120 in line for line in body)
    # Trailing items are the ones dropped, so the order is the packet's.
    assert [line for line in body] == [
        line for line in hook._format_working_set_block(packet, 9999).splitlines()[1:]
    ][: len(body)]


def test_a_ceiling_below_the_header_injects_nothing() -> None:
    assert hook._format_working_set_block(_packet(), 10) == ""
    assert hook._format_working_set_block(_packet(), 0) == ""


def test_an_empty_packet_never_injects_a_bare_header() -> None:
    empty = _packet(units=[], pointers=[], current_state=[])

    assert hook._format_working_set_block(empty, 4000) == ""


def test_the_render_ceiling_defaults_to_four_thousand_with_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hook._working_set_max_chars() == 4000

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_MAX_CHARS", "900")
    assert hook._working_set_max_chars() == 900


# --------------------------------------------------------------------------- #
# Abstentions
# --------------------------------------------------------------------------- #


def test_an_ambiguous_abstention_hands_the_senses_to_the_agent() -> None:
    block = hook._format_working_set_block(_ambiguous_packet(), 4000)

    assert block.startswith(hook._WORKING_SET_HEADER)
    assert "Northern corridor" in block
    assert "Southern corridor" in block
    assert "Knowledge Base/Notes/Insights/northern-corridor-hub.md" in block
    assert block.endswith(hook._WORKING_SET_AMBIGUITY_LINE)
    assert "anchor" in hook._WORKING_SET_AMBIGUITY_LINE
    assert "activate_context" in hook._WORKING_SET_AMBIGUITY_LINE
    assert "- unit:" not in block
    assert "- state:" not in block


def test_an_ambiguous_block_that_cannot_fit_its_instruction_injects_nothing() -> None:
    """The instruction is the point of the block: senses with no way to resolve
    them are noise the agent pays for and cannot use."""
    ceiling = len(hook._WORKING_SET_HEADER) + len(hook._WORKING_SET_AMBIGUITY_LINE)

    assert hook._format_working_set_block(_ambiguous_packet(), ceiling) == ""


@pytest.mark.parametrize(
    "reason", ["unresolved", "index_warming", "disabled", "unavailable", "withheld"]
)
def test_any_other_abstention_renders_nothing(reason: str) -> None:
    packet = _packet(
        abstained=True, reason=reason, units=[], pointers=[], current_state=[]
    )

    assert hook._format_working_set_block(packet, 4000) == ""


def test_an_abstention_leaves_exactly_the_ordinary_reminder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    _serve(
        monkeypatch,
        _packet(abstained=True, reason="unresolved", units=[], pointers=[], current_state=[]),
    )

    context = _context(_run(monkeypatch, capsys, _event(), tmp_path / "home"))

    assert context == hook.REMINDER


# --------------------------------------------------------------------------- #
# The hook end to end
# --------------------------------------------------------------------------- #


def test_the_packet_replaces_the_reminder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    _serve(monkeypatch, _packet())

    context = _context(_run(monkeypatch, capsys, _event(), tmp_path / "home"))

    assert context.startswith(hook._WORKING_SET_HEADER)
    assert hook.REMINDER not in context
    assert "depot stock: 180 kg" in context
    assert len(context) <= hook._working_set_max_chars()


def test_a_transport_failure_falls_back_to_the_reminder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    _serve(monkeypatch, None)

    context = _context(_run(monkeypatch, capsys, _event(), tmp_path / "home"))

    assert context == hook.REMINDER


def test_a_raising_transport_still_emits_the_reminder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")

    def _boom(*args, **kwargs):
        raise RuntimeError("the service exploded")

    monkeypatch.setattr(hook, "_fetch_packet_via_rest", _boom)

    context = _context(_run(monkeypatch, capsys, _event(), tmp_path / "home"))

    assert context == hook.REMINDER


def test_a_malformed_response_is_not_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hook._parse_packet({"success": True, "data": [1, 2]}) is None
    assert hook._parse_packet({"success": False, "data": {}}) is None
    assert hook._parse_packet(["a list"]) is None
    assert hook._parse_packet({"success": True, "data": {"abstained": False}}) == {
        "abstained": False
    }


def test_the_mode_stays_within_the_injection_budget() -> None:
    assert hook.INJECT_BUDGET_SECONDS == 8.0
    assert hook.REST_TIMEOUT_SECONDS <= hook.INJECT_BUDGET_SECONDS


def test_the_working_set_rung_posts_to_the_activation_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route is the only thing that distinguishes this rung from stub mode's:
    stub mode asks `ask_memory`, this asks the compiler."""
    seen: dict = {}

    class _Response:
        def getcode(self):
            return 200

        def read(self):
            return json.dumps({"success": True, "data": _packet()}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=0.0):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(hook.urllib.request, "urlopen", _urlopen)

    packet = hook._fetch_packet_via_rest(PROMPT, "sekret", "TOKEN-0", 1.0)

    assert packet is not None
    assert seen["url"].endswith("/api/activate_context")
    assert seen["body"]["turn"] == PROMPT
    assert seen["body"]["continuity"] == "TOKEN-0"


def test_no_continuity_key_is_sent_when_there_is_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    class _Response:
        def getcode(self):
            return 200

        def read(self):
            return json.dumps({"success": True, "data": _packet()}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        hook.urllib.request,
        "urlopen",
        lambda request, timeout=0.0: (
            seen.update(body=json.loads(request.data.decode("utf-8"))) or _Response()
        ),
    )

    hook._fetch_packet_via_rest(PROMPT, "sekret", "", 1.0)

    assert "continuity" not in seen["body"]


# --------------------------------------------------------------------------- #
# The gates are the ones stub mode has
# --------------------------------------------------------------------------- #


def test_prominence_off_stays_silent_in_working_set_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    seen = _serve(monkeypatch, _packet())
    monkeypatch.setenv("EXOMEM_PROMINENCE", "off")

    assert _run(monkeypatch, capsys, _event(), tmp_path / "home") == ""
    assert seen == [], "the transport must never run behind a closed gate"


def test_a_short_control_prompt_stays_silent_in_working_set_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    seen = _serve(monkeypatch, _packet())

    assert _run(monkeypatch, capsys, _event(prompt="continue"), tmp_path / "home") == ""
    assert seen == []


def test_the_session_cooldown_still_silences_the_second_prompt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    _serve(monkeypatch, _packet())
    home = tmp_path / "home"

    assert _run(monkeypatch, capsys, _event(), home) != ""
    assert _run(monkeypatch, capsys, _event(), home) == ""


def test_a_task_control_envelope_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    seen = _serve(monkeypatch, _packet())
    event = _event()
    event["hook_event_name"] = "task-notification"

    assert _run(monkeypatch, capsys, event, tmp_path / "home") == ""
    assert seen == []


# --------------------------------------------------------------------------- #
# The continuity token's client-local life
# --------------------------------------------------------------------------- #


def test_the_token_lives_beside_the_continuation_checkpoint(tmp_path: Path) -> None:
    path = hook.activation_token_path(tmp_path, "claude", SESSION)

    assert path.parent.parent == tmp_path / ".cache" / "exomem-continuation" / "claude"
    # Dot-prefixed so the checkpoint hook's prune scan, which skips dotted
    # entries, never treats the token directory as an expired session.
    assert path.parent.name.startswith(".")


def test_the_token_is_keyed_by_client_and_session(tmp_path: Path) -> None:
    one = hook.activation_token_path(tmp_path, "claude", "a")
    two = hook.activation_token_path(tmp_path, "claude", "b")
    codex = hook.activation_token_path(tmp_path, "codex", "a")

    assert len({one, two, codex}) == 3


def test_the_token_round_trips_across_two_prompts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    seen = _serve(monkeypatch, _packet(continuity="TOKEN-1"))
    home = tmp_path / "home"

    _run(monkeypatch, capsys, _event(), home)
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC", "0")
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", "0")
    _run(monkeypatch, capsys, _event(), home)

    assert [item["continuity"] for item in seen] == ["", "TOKEN-1"]


def test_a_second_session_does_not_inherit_the_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    seen = _serve(monkeypatch, _packet(continuity="TOKEN-1"))
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", "0")

    _run(monkeypatch, capsys, _event(), home)
    _run(monkeypatch, capsys, _event(session_id="another-session"), home)

    assert [item["continuity"] for item in seen] == ["", ""]


def test_an_abstained_packet_keeps_the_previous_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    """An abstention mints no token, and forgetting the last good one would cost
    continuity for the rest of the session over one unresolved turn."""
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC", "0")
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", "0")

    _serve(monkeypatch, _packet(continuity="TOKEN-1"))
    _run(monkeypatch, capsys, _event(), home)
    seen = _serve(
        monkeypatch,
        _packet(
            abstained=True,
            reason="unresolved",
            units=[],
            pointers=[],
            current_state=[],
            continuity=None,
        ),
    )
    _run(monkeypatch, capsys, _event(), home)
    _run(monkeypatch, capsys, _event(), home)

    assert [item["continuity"] for item in seen] == ["TOKEN-1", "TOKEN-1"]


@pytest.mark.parametrize(
    ("client", "payload"),
    [
        ("claude", {"hook_event_name": "PreCompact", "trigger": "manual"}),
        ("claude", {"hook_event_name": "SessionEnd"}),
        ("claude", {"hook_event_name": "SessionStart", "source": "compact"}),
        ("claude", {"hook_event_name": "SessionStart", "source": "resume"}),
        ("codex", {"hook_event_name": "PreCompact", "trigger": "auto"}),
        ("codex", {"hook_event_name": "SessionStart", "source": "resume"}),
    ],
)
def test_every_lifecycle_event_the_client_delivers_clears_the_token(
    tmp_path: Path, client: str, payload: dict
) -> None:
    home = tmp_path / client
    path = checkpoint.activation_token_path(home, client, SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("TOKEN-1", encoding="utf-8")

    checkpoint.dispatch_event(
        client,
        {**payload, "session_id": SESSION},
        environ={"EXOMEM_HOOK_HOME": str(home)},
    )

    assert not path.exists()


def test_clearing_one_session_leaves_another_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = checkpoint.activation_token_path(home, "claude", SESSION)
    theirs = checkpoint.activation_token_path(home, "claude", "other-session")
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("TOKEN-1", encoding="utf-8")
    theirs.write_text("TOKEN-2", encoding="utf-8")

    checkpoint.dispatch_event(
        "claude",
        {"hook_event_name": "SessionEnd", "session_id": SESSION},
        environ={"EXOMEM_HOOK_HOME": str(home)},
    )

    assert not mine.exists()
    assert theirs.read_text(encoding="utf-8") == "TOKEN-2"


def test_the_two_hooks_derive_the_same_token_path(tmp_path: Path) -> None:
    """The write side and the clear side live in two standalone scripts that
    cannot import each other. A drift here loses continuity silently."""
    assert hook.activation_token_path(tmp_path, "claude", SESSION) == (
        checkpoint.activation_token_path(tmp_path, "claude", SESSION)
    )
    assert hook.activation_token_path(tmp_path, "codex", "a/b c") == (
        checkpoint.activation_token_path(tmp_path, "codex", "a/b c")
    )


def test_an_unreadable_token_store_costs_continuity_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    working_set_mode: None,
) -> None:
    seen = _serve(monkeypatch, _packet())
    # A directory where a file belongs: the read and the write both raise, and
    # neither may reach the prompt.
    monkeypatch.setattr(hook, "activation_token_path", lambda *a, **k: tmp_path)

    context = _context(_run(monkeypatch, capsys, _event(), tmp_path / "home"))

    assert context.startswith(hook._WORKING_SET_HEADER)
    assert seen[0]["continuity"] == ""


# --------------------------------------------------------------------------- #
# The two copies of each hook stay byte-identical
# --------------------------------------------------------------------------- #


def test_the_hook_copies_are_byte_identical() -> None:
    assert RETRIEVE_SCRIPT.read_bytes() == PLUGIN_RETRIEVE_SCRIPT.read_bytes()
    assert (
        Path(exomem.__file__).parent / "_hooks" / "exomem_continuation_checkpoint.py"
    ).read_bytes() == PLUGIN_CHECKPOINT_SCRIPT.read_bytes()


def test_the_mode_is_documented_where_the_hook_is_installed() -> None:
    from exomem import install_hook

    source = Path(install_hook.__file__).read_text(encoding="utf-8")

    assert "working_set" in source
    assert "EXOMEM_RETRIEVE_INJECT_MAX_CHARS" in source
