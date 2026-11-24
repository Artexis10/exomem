"""The capture reminder counts a save from the write's tool result, never from the reply's words.

A plain reply no longer carries a `Saved ->` marker, so a save that only the marker proved would now
look unsaved, and a reply that merely says "Saved" would still pass for one. Each case runs the real
hook script on one Stop event at `maximal`, where any substantive turn without a save is reminded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_capture_nudge_next_turn import MARKER, _env, _run

REPLY = "We settled the lamp finish and agreed on the order of the remaining work. " + "x" * 200


def _stop(tmp_path: Path, reply: str, *, tool: str | None, tool_input: dict | None = None) -> str:
    content: list[dict] = []
    if tool:
        content.append({"type": "tool_use", "id": "t1", "name": tool, "input": tool_input or {}})
    content.append({"type": "text", "text": reply})
    rows = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "q"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": content}},
    ]
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    event = {"session_id": "session-saves", "transcript_path": str(transcript), "last_assistant_message": reply}
    return _run("exomem_capture_nudge.py", event, _env(tmp_path, EXOMEM_PROMINENCE="maximal"))


@pytest.mark.parametrize("operation", ["records.restructure", "plans.restructure"])
def test_a_restructure_is_a_save_that_its_plain_reply_does_not_need_to_mark(tmp_path: Path, operation: str) -> None:
    sent = _stop(tmp_path, REPLY, tool="mcp__exomem__manage_memory", tool_input={"operation": operation})

    assert sent == ""


def test_a_reply_that_says_saved_without_any_write_is_still_reminded(tmp_path: Path) -> None:
    sent = _stop(tmp_path, "Saved -> Knowledge Base/Notes/lamp.md. " + REPLY, tool=None)

    assert MARKER in json.loads(sent)["reason"]
