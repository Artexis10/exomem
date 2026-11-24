"""Replay a run's recorded sessions through two versions of the capture-reminder hook and count its reminders.

    PYTHONPATH=benchmarks python -m membench.outcome.incidents.reminder_replay <run root> <old hooks> <new hooks>

A change to the hook changes how often it reminds an agent to save. This replays every Stop that the run's hooked
units reached, with no model call: the transcript is rebuilt from the unit's recorded streams up to that Stop, and
each hook version gets the input the client sends (transcript path, last assistant message, session id) at the unit's
prominence level, from a fresh hook home per unit. It runs two conditions: the transcript is readable, as in an
interactive session, and it is missing, as in an unpersisted print-mode run. It prints one JSON line per arm,
condition and hook version, then each case whose counts differ between the two versions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HOOK = "exomem_capture_nudge.py"
CONDITIONS = ("readable", "missing")


def _system(event: dict, subtype: str) -> bool:
    return event.get("type") == "system" and event.get("subtype") == subtype


def stops(events: list[dict]) -> list[tuple[list[dict], bool]]:
    """Each Stop of the session: the transcript records before it, and whether the hook already blocked this turn."""
    found, seen, blocked = [], [], False
    for event in events:
        if _system(event, "init"):
            # Each print-mode invocation starts at a user prompt, which the stream does not record.
            seen.append({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "(turn)"}]}})
            blocked = False
        if _system(event, "hook_started") and event.get("hook_event") == "Stop":
            found.append((list(seen), blocked))
        if _system(event, "hook_response") and event.get("hook_event") == "Stop":
            blocked = blocked or '"decision": "block"' in (event.get("output") or "")
        if event.get("type") in ("assistant", "user"):
            seen.append(event)
    return found


def _last_text(records: list[dict]) -> str:
    for record in reversed(records):
        if record.get("type") == "assistant":
            texts = [block.get("text", "") for block in record["message"].get("content", []) if block.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def reminds(hooks: Path, level: str, transcript: Path, text: str, blocked: bool, home: Path, session: str) -> bool:
    """Whether the hook at `hooks` blocks this Stop with a reminder."""
    stop = {"session_id": session, "transcript_path": str(transcript), "last_assistant_message": text,
            "stop_hook_active": blocked, "hook_event_name": "Stop"}
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "EXOMEM_HOOK_HOME": str(home), "EXOMEM_PROMINENCE": level,
           "EXOMEM_CAPTURE_NUDGE_DELIVERY": "stop", "EXOMEM_HOOK_CLIENT": "claude"}
    done = subprocess.run([sys.executable, str(hooks / HOOK), "--client", "claude"], input=json.dumps(stop),  # noqa: S603
                          capture_output=True, text=True, env=env, timeout=30)
    return '"decision": "block"' in done.stdout


def replay(run: Path, versions: dict[str, Path], work: Path) -> tuple[dict, dict]:
    counts: dict = defaultdict(lambda: {"stops": 0, "reminders": 0})
    by_case: dict = defaultdict(dict)
    for line in (run / "results.jsonl").read_text().splitlines():
        row = json.loads(line) if line.strip() else {}
        if not row.get("hooks"):
            continue
        unit = run / "units" / row["unit"]
        streams = sorted(unit.glob("stream*.jsonl"), key=lambda path: (len(path.name), path.name))
        events = [json.loads(text) for path in streams for text in path.read_text().splitlines() if text.strip()]
        level = ((row.get("capture_prominence") or {}).get("level")
                 or (row.get("instruction_block") or {}).get("prominence") or "balanced")
        for condition in CONDITIONS:
            for version, hooks in versions.items():
                home = Path(tempfile.mkdtemp(dir=work))
                fired = 0
                for index, (records, blocked) in enumerate(stops(events)):
                    transcript = home / f"stop-{index}.jsonl"
                    if condition == "readable":
                        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
                    fired += reminds(hooks, level, transcript, _last_text(records), blocked, home, row["unit"])
                    counts[(row["arm"], condition, version)]["stops"] += 1
                counts[(row["arm"], condition, version)]["reminders"] += fired
                by_case[(row["arm"], row["case"], condition)][version] = fired
    return counts, by_case


def main(argv: list[str]) -> int:
    run, old, new = (Path(arg) for arg in argv[:3])
    with tempfile.TemporaryDirectory() as work:
        counts, by_case = replay(run, {"old": old, "new": new}, Path(work))
    for (arm, condition, version), value in sorted(counts.items()):
        print(json.dumps({"arm": arm, "transcript": condition, "hook": version, **value}))
    for (arm, case, condition), value in sorted(by_case.items()):
        if len(set(value.values())) > 1:
            print(json.dumps({"differs": {"arm": arm, "case": case, "transcript": condition, **value}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
