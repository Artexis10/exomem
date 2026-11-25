"""Model-free method check: which methods the product serves for each no-magic-word case, scored against its key.

Each arm is a checkout and an optional method-fit placement file. For every arm this seeds the case folder's seed vault
with the arm's own scaffold, starts the arm's own stdio server on it, and sends each case's trigger turn to
`recall_context` with the case's earlier turns as conversation excerpts. It records the skills served with
`selection: "method_fit"` and compares them with the key's `methods`. A key's `destination` is compared with any
collection the same response selected. No actor runs; a placement's model is the only model called.

    PYTHONPATH=benchmarks python -m import_guidance.method_check --arm base=<checkout> \\
        --arm local=<checkout>:<placement.json> --cases <case folder> --out <dir outside every repository>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from import_guidance import session_close

SEED_WORKER = Path(__file__).with_name("session_seed.py")
#: Conversation excerpt bound of `recall_context` (`conversation.recent[].text`, user role).
EXCERPT_CHARS = 600
#: How long a fresh server may warm its index before the run gives up.
READY_SECONDS = 600.0


@dataclass(frozen=True)
class Arm:
    label: str
    root: Path
    placement: Path | None


def parse_arm(value: str) -> Arm:
    label, _, rest = value.partition("=")
    root, _, placement = rest.partition(":")
    if not label or not root:
        raise argparse.ArgumentTypeError("an arm is LABEL=CHECKOUT[:PLACEMENT.json]")
    return Arm(label, Path(root).resolve(), Path(placement).resolve() if placement else None)


def arm_env(arm: Arm, work: Path) -> dict[str, str]:
    """The arm's interpreter path and a private state tree; no real vault, service state or credential is reachable."""
    state = work / "state"
    env = {key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_") and key != "VIRTUAL_ENV"}
    env.update({
        "PYTHONPATH": str(arm.root / "src"), "EXOMEM_VAULT_PATH": str(work / "vault"), "EXOMEM_STATE_ROOT": str(state),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state / "lease"), "EXOMEM_CONFIG_PATH": str(work / "exomem-config.json"),
        "EXOMEM_LOG_DIR": str(work / "logs"), "XDG_STATE_HOME": str(work / "xdg"), "EXOMEM_ACTIVATION_PACKET": "maps",
        "EXOMEM_DISABLE_EMBEDDINGS": "1", "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1", "EXOMEM_DISABLE_QUERY_LOG": "1",
    })
    if arm.placement is not None:
        env["EXOMEM_METHOD_FIT_CONFIG"] = str(arm.placement)
    return env


def interpreter(arm: Arm) -> str:
    local = arm.root / ".venv" / "bin" / "python"
    return str(local) if local.is_file() else sys.executable


async def _ask(arm: Arm, env: dict[str, str], work: Path, turns: list[dict]) -> list[dict]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(command=interpreter(arm), args=["-m", "exomem", "--transport", "stdio"], env=env, cwd=str(work))
    answers = []
    async with Client(transport, timeout=300) as client:
        # A fresh server warms its index first and abstains meanwhile; a case asked then would score the warm-up.
        deadline = time.monotonic() + READY_SECONDS
        while True:
            probe = json.loads((await client.call_tool("recall_context", {"turn": "ready?"})).content[0].text)
            if (probe.get("abstention") or {}).get("reason") != "index_warming":
                break
            if time.monotonic() > deadline:
                raise SystemExit(f"{arm.label}: the server was still warming after {READY_SECONDS:.0f} s")
            await asyncio.sleep(5)
        for request in turns:
            started = time.monotonic()
            reply = await client.call_tool("recall_context", request["arguments"])
            packet = json.loads(reply.content[0].text)
            task = packet.get("task_context") or {}
            methods = (task.get("methods") or {}).get("items") or []
            answers.append({
                "case": request["case"],
                "selected": [item.get("source_ref") for item in methods if item.get("selection") == "method_fit"],
                "kinds": {item.get("source_ref"): item.get("kind") for item in methods},
                "contract_complete": {item.get("source_ref"): (item.get("contract") or {}).get("complete")
                                      for item in methods if item.get("selection") == "method_fit"},
                "served_methods": [item.get("read_ref") or item.get("source_ref") for item in methods],
                "method_fit": task.get("method_fit"), "task_state": task.get("state"),
                "abstention": (packet.get("abstention") or {}).get("reason"),
                "seconds": round(time.monotonic() - started, 3),
            })
    return answers


def run_arm(arm: Arm, cases: list, keys: dict, case_dir: Path, out: Path) -> list[dict]:
    work = out / "work" / arm.label
    if work.exists():
        raise SystemExit(f"{work} exists; use a fresh --out")
    work.mkdir(parents=True)
    env = arm_env(arm, work)
    seeds = {case.seed for case in cases}
    if len(seeds) != 1:
        raise SystemExit("one seed per run: every case must share the seed vault")
    subprocess.run(  # noqa: S603
        [interpreter(arm), str(SEED_WORKER), str(next(iter(seeds))), str(work / "vault"), str(arm.root)],
        env=env, cwd=work, check=True, timeout=600, capture_output=True,
    )
    turns = []
    for case in cases:
        key = keys[case.id]
        trigger = key["trigger_turn"]
        recent = [{"role": "user", "text": turn[:EXCERPT_CHARS]} for turn in case.turns[:trigger]]
        arguments = {"turn": case.turns[trigger], "max_chars": 6000, "client": "method-check"}
        if recent:
            arguments["conversation"] = {"recent": recent[-6:]}
        turns.append({"case": case.id, "arguments": arguments})
    answers = asyncio.run(_ask(arm, env, work, turns))
    rows = []
    for case, answer in zip(cases, answers, strict=True):
        key = keys[case.id]
        kb = "Knowledge Base/"
        keyed = sorted(kb + path for path in key["methods"])
        skills = sorted(path for path in answer["selected"] if answer["kinds"].get(path) == "skill")
        collections = [path for path in answer["selected"] if answer["kinds"].get(path) == "collection"]
        destination = key.get("destination")
        rows.append({
            "arm": arm.label, **answer, "keyed": keyed, "selected_skills": skills, "exact": skills == keyed,
            "false_selections": len(set(skills) - set(keyed)), "missed": len(set(keyed) - set(skills)),
            "destination": destination,
            "destination_selected": (kb + destination in collections) if destination else None,
        })
    return rows


def paired_interval(differences: list[int], draws: int = 10000, seed: int = 20261122) -> tuple[float, float]:
    """The 95% bootstrap interval of the mean per-case difference."""
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(differences) for _ in differences) / len(differences) for _ in range(draws))
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def report(rows: list[dict], arms: list[str]) -> dict:
    summary: dict = {"arms": {}, "paired": {}}
    by_arm = {arm: {row["case"]: row for row in rows if row["arm"] == arm} for arm in arms}
    for arm, cases in by_arm.items():
        summary["arms"][arm] = {
            "cases": len(cases), "exact": sum(row["exact"] for row in cases.values()),
            "false_selections": sum(row["false_selections"] for row in cases.values()),
            "missed": sum(row["missed"] for row in cases.values()),
            "unavailable": sum((row["method_fit"] or {}).get("state") == "unavailable" for row in cases.values()),
            "median_seconds": sorted(row["seconds"] for row in cases.values())[len(cases) // 2] if cases else None,
        }
    base = arms[0]
    for arm in arms[1:]:
        shared = sorted(set(by_arm[base]) & set(by_arm[arm]))
        differences = [int(by_arm[arm][case]["exact"]) - int(by_arm[base][case]["exact"]) for case in shared]
        if differences:
            low, high = paired_interval(differences)
            summary["paired"][f"{arm} - {base}"] = {
                "cases": len(differences), "mean_difference": sum(differences) / len(differences),
                "interval_95": [low, high],
            }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", action="append", type=parse_arm, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    # The seed worker runs from each arm's work folder, so the case folder must not depend on the caller's cwd.
    args.cases = args.cases.resolve()
    cases = session_close.load_cases(args.scenario, args.cases)
    keys = {case.id: json.loads((args.cases / "answer-keys" / f"{case.id}.json").read_text(encoding="utf-8")) for case in cases}
    missing = [case.id for case in cases if "methods" not in keys[case.id]]
    if missing:
        raise SystemExit(f"keys without methods: {missing}")
    args.out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for arm in args.arm:
        rows.extend(run_arm(arm, cases, keys, args.cases, args.out))
        print(f"{arm.label}: {sum(row['exact'] for row in rows if row['arm'] == arm.label)}/{len(cases)} exact", flush=True)
    (args.out / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    summary = report(rows, [arm.label for arm in args.arm])
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
