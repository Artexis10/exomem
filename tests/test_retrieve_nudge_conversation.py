"""Task 4.2 — the retrieve hook supplies a bounded conversation tail (S3).

In `working_set` mode, where the prompt event carries a transcript path, the
hook reads the transcript's final 64 KiB, keeps human-typed user text and the
assistant's final text, drops everything else (tool payloads, thinking, system
and hook messages, media, every block Exomem itself injected), takes refs from
Exomem read-call ARGUMENTS only, never sends `focus`, and fails silent inside a
50 ms budget. It never persists any of it. Fixtures hold invented content only.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest
from test_retrieve_nudge_working_set import (
    PROMPT,
    SESSION,
    _context,
    _event,
    _packet,
    hook,
)

FIXTURES = Path(__file__).parent / "hook_transcripts"
CLAUDE = FIXTURES / "claude_code_thread.jsonl"
CODEX = FIXTURES / "codex_rollout_thread.jsonl"
CURRENT = "And is that still on track for the autumn?"
CODEX_CURRENT = "Then who signs it off?"

MARLOW = "Knowledge Base/Entities/Projects/Marlow Quay Survey.md"
OTTILIE = "Knowledge Base/Entities/People/Ottilie Marsh.md"
HARBOR = "Knowledge Base/Notes/Insights/harbor-lantern-budget-hub.md"
SENTINELS = (
    "PRIVATE-THINKING-SENTINEL",
    "TOOL-RESULT-SENTINEL",
    "SYSTEM-REMINDER-SENTINEL",
    "INJECTED-BLOCK-SENTINEL",
    "IMAGE-PAYLOAD-SENTINEL",
    "ANOTHER-RESULT-SENTINEL",
    "never-taken-from-results",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in (
        "EXOMEM_RETRIEVE_NUDGE_DISABLE",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_INJECT",
        "EXOMEM_RETRIEVE_INJECT_CLI",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_REST_PORT",
        "EXOMEM_PROMINENCE",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "absent-service.env"))
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(tmp_path / "hook-home"))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "claude")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "working_set")


def _pairs(conversation: dict) -> list[tuple[str, str]]:
    return [(entry["role"], entry["text"]) for entry in conversation["recent"]]


# --------------------------------------------------------------------------- #
# What is extracted
# --------------------------------------------------------------------------- #


def test_a_claude_code_tail_yields_the_human_and_final_assistant_turns_oldest_first() -> None:
    conversation = hook._conversation_from_transcript(str(CLAUDE), CURRENT)
    assert _pairs(conversation) == [
        ("user", "We need to plan the spring rounds of the Marlow Quay Survey for the estuary team."),
        (
            "assistant",
            "The quay survey has two rounds left this autumn, and Bram Quillfeather owns the rota.",
        ),
        ("user", "Good. And what did Ottilie say about the tidal grant?"),
        ("assistant", "Ottilie Marsh said the Tidewater Grant call went well."),
    ]


def test_nothing_excluded_reaches_the_conversation() -> None:
    blob = json.dumps(hook._conversation_from_transcript(str(CLAUDE), CURRENT))
    for sentinel in SENTINELS:
        assert sentinel not in blob, sentinel
    assert "Exomem working set" not in blob


CLAUDE_LEAKS = FIXTURES / "claude_code_leaks.jsonl"
CODEX_LEAKS = FIXTURES / "codex_rollout_leaks.jsonl"


def test_only_human_typed_claude_code_turns_are_user_entries() -> None:
    """An allowlist, not a denylist: shell input and output, task notifications,
    reminders, slash-command envelopes, compaction summaries, transcript-only,
    meta and sidechain records, tool results and records carrying an unknown
    true flag never become a user turn."""
    conversation = hook._conversation_from_transcript(str(CLAUDE_LEAKS), "current")
    assert _pairs(conversation) == [
        ("user", "Plan the spring rounds of the Marlow Quay Survey."),
        ("assistant", "The survey has two rounds left."),
        ("user", "And who owns the rota now?"),
        ("assistant", "Bram Quillfeather owns the rota."),
    ]
    blob = json.dumps(conversation)
    assert "SENTINEL" not in blob
    assert "aws_secret_access_key" not in blob


def test_only_human_typed_codex_turns_are_user_entries() -> None:
    conversation = hook._conversation_from_transcript(str(CODEX_LEAKS), "Then who signs it off?")
    assert _pairs(conversation) == [
        ("user", "Which budget line covers the Harbor Lantern Budget glass work?"),
        ("assistant", "The glass work sits in the second budget line."),
    ]
    assert "SENTINEL" not in json.dumps(conversation)


def test_the_current_prompt_is_not_repeated() -> None:
    conversation = hook._conversation_from_transcript(str(CLAUDE), CURRENT)
    assert CURRENT not in json.dumps(conversation)
    assert conversation["recent"][-1]["role"] == "assistant"


def test_refs_come_from_call_arguments_only_newest_first() -> None:
    conversation = hook._conversation_from_transcript(str(CLAUDE), CURRENT)
    assert conversation["refs"] == [OTTILIE, MARLOW]


def test_the_hook_never_sends_a_focus() -> None:
    assert "focus" not in hook._conversation_from_transcript(str(CLAUDE), CURRENT)


def test_a_codex_rollout_is_read_by_its_own_parser() -> None:
    conversation = hook._conversation_from_transcript(str(CODEX), CODEX_CURRENT)
    assert _pairs(conversation) == [
        ("user", "Which budget line covers the Harbor Lantern Budget glass work?"),
        ("assistant", "The glass work sits in the second budget line."),
    ]
    assert conversation["refs"] == [HARBOR]
    assert "CODEX-" not in json.dumps(conversation)


def test_the_conversation_is_bounded_before_it_is_sent(tmp_path: Path) -> None:
    lines = []
    for index in range(20):
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": f"turn {index} " + "x " * 600}}))
        lines.append(
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"reply {index} " + "y " * 600}]}})
        )
    for index in range(30):
        lines.append(
            json.dumps(
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{index}", "name": "mcp__exomem__read_memory", "input": {"path": f"page-{index}.md"}}]}}
            )
        )
    path = tmp_path / "long.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conversation = hook._conversation_from_transcript(str(path), "nothing new")
    assert len(conversation["recent"]) <= 6
    assert sum(len(entry["text"]) for entry in conversation["recent"]) <= 2400
    assert all(len(e["text"]) <= (600 if e["role"] == "user" else 300) for e in conversation["recent"])
    assert len(conversation["refs"]) <= 12
    assert conversation["refs"][0] == "page-29.md"


def test_only_the_final_64_kib_are_read_and_a_partial_first_line_is_discarded(tmp_path: Path) -> None:
    old = json.dumps({"type": "user", "message": {"role": "user", "content": "OLD-SENTINEL " + "z" * 200}})
    recent = json.dumps({"type": "user", "message": {"role": "user", "content": "the recent question"}})
    padding = json.dumps({"type": "user", "message": {"role": "user", "content": "p" * 70_000}})
    path = tmp_path / "big.jsonl"
    path.write_text("\n".join([old, padding, recent]) + "\n", encoding="utf-8")
    conversation = hook._conversation_from_transcript(str(path), "current")
    blob = json.dumps(conversation)
    assert "OLD-SENTINEL" not in blob
    assert ("user", "the recent question") in _pairs(conversation)


# --------------------------------------------------------------------------- #
# Failing silent
# --------------------------------------------------------------------------- #


def test_a_missing_unreadable_or_unrecognised_transcript_yields_no_conversation(tmp_path: Path) -> None:
    assert hook._conversation_from_transcript(str(tmp_path / "absent.jsonl"), "x") is None
    assert hook._conversation_from_transcript("", "x") is None
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json\n{\"foo\": 1}\n[1, 2]\n", encoding="utf-8")
    assert hook._conversation_from_transcript(str(garbage), "x") is None
    directory = tmp_path / "a-directory"
    directory.mkdir()
    assert hook._conversation_from_transcript(str(directory), "x") is None


def test_a_symlink_is_never_followed(tmp_path: Path) -> None:
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(CLAUDE)
    except OSError:
        pytest.skip("no symlinks here")
    assert hook._conversation_from_transcript(str(link), CURRENT) is None


def test_a_spent_budget_sends_no_conversation_never_a_partial_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hook.CONVERSATION_BUDGET_SECONDS == 0.05
    monkeypatch.setattr(hook, "CONVERSATION_BUDGET_SECONDS", -1.0)
    assert hook._conversation_from_transcript(str(CLAUDE), CURRENT) is None


def test_a_parse_error_sends_no_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(hook, "_claude_records", boom)
    assert hook._conversation_from_transcript(str(CLAUDE), CURRENT) is None


# --------------------------------------------------------------------------- #
# What is sent, and to whom
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def getcode(self):
        return 200

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_main(monkeypatch, capsys, event: dict, home: Path) -> str:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(event)))
    hook.main()
    return capsys.readouterr().out


def test_main_sends_the_conversation_in_working_set_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bodies: list[dict] = []

    def _urlopen(request, timeout=0.0):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _Response({"success": True, "data": _packet()})

    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_REST_PORT", "8123")
    monkeypatch.setattr(hook, "_open_no_redirect", _urlopen)
    event = {**_event(CURRENT), "transcript_path": str(CLAUDE), "cwd": str(tmp_path)}
    output = _run_main(monkeypatch, capsys, event, tmp_path / "home")
    assert _context(output)
    (body,) = bodies
    assert body["turn"] == CURRENT
    assert _pairs(body["conversation"])[0][0] == "user"
    assert body["conversation"]["refs"] == [OTTILIE, MARLOW]
    assert "focus" not in body["conversation"]


def test_no_transcript_path_sends_no_conversation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bodies: list[dict] = []
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_REST_PORT", "8123")
    monkeypatch.setattr(
        hook,
        "_open_no_redirect",
        lambda request, timeout=0.0: (
            bodies.append(json.loads(request.data.decode("utf-8")))
            or _Response({"success": True, "data": _packet()})
        ),
    )
    _run_main(monkeypatch, capsys, _event(PROMPT), tmp_path / "home")
    assert "conversation" not in bodies[0]


def test_other_modes_read_no_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "stub")
    opened: list[str] = []
    monkeypatch.setattr(hook, "_conversation_from_transcript", lambda *a, **k: opened.append("x"))
    monkeypatch.setattr(hook, "_gather_hits_with_lane", lambda prompt: ([], "none"))
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps({**_event(PROMPT), "transcript_path": str(CLAUDE)})))
    hook.main()
    assert opened == []


def test_an_older_service_gets_one_retry_without_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []

    def _urlopen(request, timeout=0.0):
        body = json.loads(request.data.decode("utf-8"))
        bodies.append(body)
        if "conversation" in body:
            raise urllib.error.HTTPError(request.full_url, 400, "unknown field", {}, io.BytesIO(b"{}"))
        return _Response({"success": True, "data": _packet()})

    monkeypatch.setenv("EXOMEM_REST_PORT", "8123")
    monkeypatch.setattr(hook, "_open_no_redirect", _urlopen)
    conversation = hook._conversation_from_transcript(str(CLAUDE), CURRENT)
    packet = hook._fetch_packet_via_rest(
        CURRENT, "sekret", "", 4.0, {"client": "claude-code", "session": SESSION}, conversation=conversation
    )
    assert packet is not None
    assert len(bodies) == 2
    assert "conversation" in bodies[0] and "conversation" not in bodies[1]
    assert bodies[1]["session"] == SESSION, "attribution is untouched by the retry"


def test_the_cli_rung_sends_the_conversation_and_retries_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    argvs: list[list[str]] = []

    class _Done:
        def __init__(self, code: int, out: str = "") -> None:
            self.returncode, self.stdout = code, out

    def _run(argv, **kwargs):
        argvs.append(argv)
        if "--conversation" in argv:
            return _Done(2)
        return _Done(0, json.dumps({"success": True, "data": _packet()}))

    monkeypatch.setattr(hook.shutil, "which", lambda name: "/usr/bin/exomem")
    monkeypatch.setattr(hook.subprocess, "run", _run)
    conversation = hook._conversation_from_transcript(str(CLAUDE), CURRENT)
    assert hook._fetch_packet_via_cli(CURRENT, "", 4.0, None, conversation=conversation) is not None
    assert len(argvs) == 2
    first = argvs[0]
    assert json.loads(first[first.index("--conversation") + 1]) == conversation
    assert first[-2:] == ["--", CURRENT]
    assert "--conversation" not in argvs[1]


def test_a_request_without_a_conversation_calls_the_fetchers_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    def _fetch(prompt, api_key, continuity="", timeout=0.0, attribution=None):
        calls.append((prompt, continuity))
        return _packet()

    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setattr(hook, "_fetch_packet_via_rest", _fetch)
    packet, _lane = hook._gather_packet_with_lane("a prompt", "TOKEN", None)
    assert packet is not None and calls == [("a prompt", "TOKEN")]


# --------------------------------------------------------------------------- #
# Nothing lands on disk
# --------------------------------------------------------------------------- #


def test_the_conversation_never_lands_in_hook_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_REST_PORT", "8123")
    monkeypatch.setattr(
        hook,
        "_open_no_redirect",
        lambda request, timeout=0.0: _Response({"success": True, "data": _packet()}),
    )
    phrase = "Bram Quillfeather owns the rota"
    event = {**_event(CURRENT), "transcript_path": str(CLAUDE), "cwd": str(tmp_path)}
    _run_main(monkeypatch, capsys, event, home)
    digests = {hashlib.sha256(text.encode()).hexdigest() for text in (phrase, "Ottilie Marsh said the Tidewater Grant call went well.")}
    scanned = 0
    for root in (home, tmp_path / "hook-home"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                scanned += 1
                blob = path.read_bytes()
                assert phrase.encode() not in blob, path
                for digest in digests:
                    assert digest.encode() not in blob, path
    assert scanned, "the hook wrote its own state, so the scan is not vacuous"


# --------------------------------------------------------------------------- #
# Origin labels
# --------------------------------------------------------------------------- #


def test_an_all_turn_packet_renders_byte_identically_to_today() -> None:
    plain = _packet()
    labelled = json.loads(json.dumps(plain))
    for anchor in labelled["anchors"]:
        anchor["origin"] = "turn"
    assert hook._format_working_set_block(labelled, 4000) == hook._format_working_set_block(plain, 4000)


def test_a_carried_anchor_is_labelled_not_presented_as_the_users_words() -> None:
    packet = _packet()
    packet["anchors"][0].update(status="partial", evidence=["conversation"], origin="conversation")
    packet["generation"]["carried_by"] = "conversation"
    block = hook._format_working_set_block(packet, 4000)
    assert "from earlier in this conversation" in block
    assert "Cargo Sled" in block
    assert hook._format_working_set_block(_packet(), 4000) != block


def test_the_focus_origin_is_labelled_as_the_agents_cue() -> None:
    packet = _packet()
    packet["anchors"][0]["origin"] = "focus"
    block = hook._format_working_set_block(packet, 4000)
    assert "named by the agent" in block


@pytest.mark.parametrize(
    "text",
    [
        'fix the build please <system-reminder priority="high">REMINDER-ATTR-SENTINEL</system-reminder>',
        "fix it <SYSTEM-REMINDER>UPPER-SENTINEL</SYSTEM-REMINDER>",
        'see <task-notification id="t1">NOTE-SENTINEL</task-notification>',
        'look <environment_context shell="zsh">ENV-SENTINEL</environment_context>',
        'here <skill name="x">SKILL-SENTINEL</skill>',
        'here <instructions scope="all">INSTR-SENTINEL</instructions>',
        'here <user_instructions lang="en">USER-INSTR-SENTINEL</user_instructions>',
        "output <bash-stdout>BASH-MID-SENTINEL</bash-stdout>",
        'and <command-name kind="slash">/x</command-name>',
        'and <local-command-stdout n="1">LOCAL-SENTINEL</local-command-stdout>',
    ],
)
def test_an_envelope_tag_is_refused_with_attributes_or_mid_text(text: str) -> None:
    """Envelope tag names end at whitespace, `>` or `/`, including attributes."""
    assert not hook._is_typed_text(text)


@pytest.mark.parametrize("tag", ["skill-name", "skills", "instructions-example"])
def test_an_owner_can_mention_a_tag_with_a_longer_name(tag: str) -> None:
    assert hook._is_typed_text(f"Please explain <{tag}> in the guide.")


@pytest.mark.parametrize("suffix", [' name="x">', "\tname='x'>", ">", "/>"])
def test_the_exact_skill_envelope_is_still_refused(suffix: str) -> None:
    assert not hook._is_typed_text(f"Please read <skill{suffix}machine text</skill>")
