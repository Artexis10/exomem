#!/usr/bin/env python3
"""Bytes Exomem injects into a coding agent's context, and a 50-turn estimate.

Read-only. Measures what can be measured exactly (hook text constants, the
pinned MCP tool schemas, the plugin skills) and models the rest with the hooks'
own gating rules over stated assumptions (`--turns`, `--step`), because a real
session's cadence depends on the agent and the user.

    python scripts/context-footprint.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "src" / "exomem" / "_hooks"
SKILLS = ROOT / "plugins" / "claude-code" / "skills"

#: (capture min chars, capture cooldown s), (episode substantive turns, cooldown s),
#: (retrieve session cooldown s, retrieve client-wide cooldown s): mirrors the
#: `_PROMINENCE_PRESETS` / `_EPISODE_ASK_PRESETS` tables in the hook scripts.
CADENCE = {
    "balanced": ((300, 300), (6, 1200), (300, 900)),
    "maximal": ((120, 60), (3, 600), (0, 0)),
}


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fires(level: str, turns: int, step: int) -> tuple[int, int, int]:
    """Capture, episode and retrieve nudges over a session with no records made.

    Assumptions: one prompt and one Stop per turn; every tenth turn is a short
    control prompt (retrieve stays silent); 70% of balanced turns end in text long
    enough to count as substantive (90% at maximal); the agent writes nothing.
    """
    (_, cap_cool), (ep_turns, ep_cool), (ret_cool, glob_cool) = CADENCE[level]
    since = capture = episode = retrieve = 0
    last_cap = last_ep = last_ret = last_glob = -(10**9)
    for i in range(turns):
        now = i * step
        if i % 10 != 5 and now - last_ret >= ret_cool and now - last_glob >= glob_cool:
            retrieve += 1
            last_ret = last_glob = now
        substantive = (i % 10) != 9 if level == "maximal" else (i % 10) not in (3, 7, 9)
        since += substantive
        if since >= ep_turns and now - last_ep >= ep_cool:
            episode += 1
            last_ep = now
        elif substantive and now - last_cap >= cap_cool:
            capture += 1
            last_cap = now
    return capture, episode, retrieve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=50)
    parser.add_argument("--step", type=int, default=144, help="seconds between turns")
    args = parser.parse_args()

    capture = load("exomem_capture_nudge")
    retrieve = load("exomem_retrieve_nudge")
    key = "ep-" + "0" * 32
    sizes = {
        "capture check (Stop)": len(json.dumps({"decision": "block", "reason": capture.REMINDER_SHORT})),
        "episode check (Stop)": len(
            json.dumps({"decision": "block", "reason": capture.EPISODE_ASK.replace("{key}", key)})
        ),
        "retrieval check (UserPromptSubmit)": len(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": retrieve.REMINDER,
                    }
                }
            )
        ),
        "working-set header (opt-in)": len(retrieve._WORKING_SET_HEADER),
        "working-set ceiling (opt-in)": retrieve._WORKING_SET_MAX_CHARS,
    }
    short = {
        "retrieval pointer (maximal)": len(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": retrieve.REMINDER_POINTER,
                    }
                }
            )
        ),
    }
    print("hook injections, bytes each")
    for name, size in {**sizes, **short}.items():
        print(f"  {name:38}{size:>8}")

    print(f"\nfires in {args.turns} turns, {args.step}s apart (modelled)")
    for level in CADENCE:
        cap, ep, ret = fires(level, args.turns, args.step)
        stop = cap * sizes["capture check (Stop)"] + ep * sizes["episode check (Stop)"]
        prompt = ret * sizes["retrieval check (UserPromptSubmit)"]
        print(f"  {level:9} capture={cap:<3} episode={ep:<3} retrieve={ret:<3} stop={stop:>7} B  prompt={prompt:>7} B")
        # After `shrink-bootstrap`: the retrieval reminder once per session and after a
        # compaction, plus a pointer on every other prompt at maximal. Every Stop check
        # is already the short one (`shorten-stop-hook-blocks`), so `stop` stands as is.
        compactions = 1
        prompts = args.turns - args.turns // 10
        full_reminders = 1 + compactions
        pointers = prompts - full_reminders if level == "maximal" else 0
        prompt_after = (
            full_reminders * sizes["retrieval check (UserPromptSubmit)"]
            + pointers * short["retrieval pointer (maximal)"]
        )
        print(f"  {'':9} prompt after: {prompt_after:>7} B (one compaction)")

    schemas = json.loads((ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json").read_text())
    description = sum(len(t["description"]) for t in schemas.values())
    total = sum(len(json.dumps(t)) for t in schemas.values())
    print(f"\nMCP tool schemas: {len(schemas)} tools, {total} B ({description} B tool descriptions), resent per turn by clients that do not defer them")

    front = 0
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        match = re.match(r"---\n(.*?)\n---", skill.read_text(), re.S)
        front += len(match.group(1)) if match else 0
    print(f"plugin skill front matter: {front} B across {len(list(SKILLS.glob('*/SKILL.md')))} skills, listed every turn")


if __name__ == "__main__":
    main()
