"""Judge items, the judge runs, agreement, and the unblinding with its tie-break.

Design, "Measures and judging": one item per (case, arm, repeat) under an
opaque shuffled id; every arm's item shows the same view (the user turns, the
key's required items, forbidden claims and successors, and the answer) with
``exomem://`` refs replaced by opaque tokens and every arm's Exomem tool
names by one neutral token; the arm key lives apart, under
``unblinding/``, and no judge lane reads it. The judge answers through a
structured-output schema. A random fifth is judged twice under fresh ids,
mixed in. The tie-break between two judges runs here, in a script, only after
every verdict is in and the arm key is checked against the item ids.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import environment_floor

from membench.judge.blinding import (
    SourceNumbering,
    deterministic_permutation,
    shuffled,
    tokenize_exomem_refs,
)

from . import protocol
from .cases import Case
from .session import CLIENT_PINS, launched_argv
from .state import Layout

TEXTS = Path(__file__).resolve().parent / "texts"

#: The fields a judge item carries, and nothing else: no arm, no session, no
#: repeat, no ref.
ITEM_FIELDS: tuple[str, ...] = (
    "item_id",
    "turns",
    "scored_turn",
    "required",
    "forbidden",
    "expects_memory",
    "answer",
)

#: The judge routes the protocol names: the Codex ``-sol`` judge, and the
#: other family's judge for the recovery path (or when Codex has no quota).
ROUTES: tuple[str, ...] = ("codex", "claude")

#: What each route's judge sees beyond its item, recorded with the route so a
#: run report names it. Measured from the judge's own rollout (Codex 0.163.0).
ROUTE_LIMITATIONS: dict[str, tuple[str, ...]] = {
    "codex": (
        "The judge receives the Codex home's global AGENTS.md, the client's base, "
        "permissions and multi-agent instructions and its environment context; no "
        "flag of this version drops them. The text is the same for every item.",
        "A verdict whose event stream holds any item other than a message, "
        "reasoning or an error is refused, so neither a tool call nor a spawned "
        "sub-agent can read the arm key into a verdict.",
    ),
    "claude": (
        "The judge receives the client's base system prompt and its environment "
        "block; no flag of this version drops them. The text is the same for "
        "every item.",
        "Its only tool is the client's structured-output tool, so it can neither "
        "read the arm key nor spawn a sub-agent. It loads no MCP server and no "
        "memory file.",
        "Its model is the actor's family.",
    ),
}

#: Harness knob: attempts per item before the judge stage pauses, so a spent
#: judge quota stops the stage and a rerun resumes it.
_ITEM_ATTEMPTS = 3
#: Linux caps one argv string at 128 KiB (MAX_ARG_STRLEN); the prompt is one.
_PROMPT_MAX_BYTES = 120 * 1024


class JudgeError(RuntimeError):
    pass


class UnblindingError(RuntimeError):
    """Unblinding refused: verdicts are missing, or the arm key does not match."""


def judge_schema() -> dict:
    """The verdict schema, in the strict form both structured-output routes accept."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["item_id", "required", "forbidden", "no_memory"],
        "properties": {
            "item_id": {"type": "string"},
            "required": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "verdict"],
                    "properties": {
                        "label": {"type": "string"},
                        "verdict": {"type": "string", "enum": list(protocol.REQUIRED_VERDICTS)},
                    },
                },
            },
            "forbidden": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "verdict"],
                    "properties": {
                        "label": {"type": "string"},
                        "verdict": {"type": "string", "enum": list(protocol.FORBIDDEN_VERDICTS)},
                    },
                },
            },
            "no_memory": {
                "type": "object",
                "additionalProperties": False,
                "required": ["asserts_unsupplied_user_facts", "span"],
                "properties": {
                    "asserts_unsupplied_user_facts": {"type": ["boolean", "null"]},
                    "span": {"type": ["string", "null"]},
                },
            },
        },
    }


def _secret(seed: int, inputs_digest: str) -> str:
    return hashlib.sha256(f"switch-eval-items\0{seed}\0{inputs_digest}".encode()).hexdigest()


def _opaque(secret: str, *parts: str) -> str:
    return "item-" + hashlib.sha256("\0".join((secret, *parts)).encode()).hexdigest()[:16]


def case_labels(case: Case) -> dict[str, str]:
    """Neutral labels for the key's items: ``r1``.. for required, ``f1``.. for forbidden."""

    labels = {f"r{n}": item.item for n, item in enumerate(case.required, start=1)}
    labels.update({f"f{n}": item.item for n, item in enumerate(case.forbidden, start=1)})
    return labels


#: The one neutral token that stands for every Exomem tool name in the judge view.
TOOL_TOKEN = "[tool]"


def exomem_tool_names(
    layout: Layout, *, arms: Sequence[str], trees: Sequence[str]
) -> tuple[str, ...]:
    """Every tool name any arm's actor was shown, bare and client-prefixed.

    The union of the tool lists the state stage captured from the server for
    every arm and tree, each also in the client's ``mcp__<server>__<tool>``
    form for every server in the arm's MCP config. A per-arm list would leave
    P's mention of a tool only A and M have in the judge view.
    """

    names: set[str] = set()
    for arm in arms:
        config = layout.arm_dir(arm) / "session-inputs" / "mcp.json"
        servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
        for tree in trees:
            surface = json.loads(
                (layout.arm_tree(arm, tree) / "surface.json").read_text(encoding="utf-8")
            )
            for shown in surface["shown"].values():
                for tool in shown["tools"]:
                    names.add(tool["name"])
                    names.update(f"mcp__{server}__{tool['name']}" for server in servers)
    return tuple(sorted(names))


def mask_tool_names(text: str, names: Sequence[str]) -> str:
    """``text`` with each of ``names`` replaced by :data:`TOOL_TOKEN`."""

    if not names:
        return text
    # Identifier boundaries, so a longer identifier that contains a name stays
    # whole; the names are the captured tool lists, not a word list.
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.sub(rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])", TOOL_TOKEN, text)


def item_payload(case: Case, answer: str, item_id: str, *, tool_names: Sequence[str]) -> dict:
    """One judge item. One numbering per item, so a token carries nothing across items."""

    numbering = SourceNumbering()

    def blind(text: str) -> str:
        return mask_tool_names(tokenize_exomem_refs(text, numbering), tool_names)

    return {
        "item_id": item_id,
        "turns": [blind(turn) for turn in case.turns[: case.scored_turn + 1]],
        "scored_turn": case.scored_turn + 1,
        "required": [
            {
                "label": f"r{n}",
                "statement": blind(item.statement),
                "accepted_successors": [
                    blind(s.statement) for s in case.successors.get(item.item, ())
                ],
            }
            for n, item in enumerate(case.required, start=1)
        ],
        "forbidden": [
            {"label": f"f{n}", "claim": blind(item.claim)}
            for n, item in enumerate(case.forbidden, start=1)
        ],
        "expects_memory": case.expects_memory,
        "answer": blind(answer),
    }


@dataclass(frozen=True)
class ItemSet:
    items: tuple[dict, ...]
    arm_key: Mapping[str, dict]
    labels: Mapping[str, Mapping[str, str]]


def build_items(
    cases: Sequence[Case],
    sessions: Sequence[Mapping],
    *,
    seed: int,
    inputs_digest: str,
    tool_names: Sequence[str],
) -> ItemSet:
    """Every judged item, the re-judged fifth under fresh ids, shuffled together.

    Only sessions with an answer are judged: an actor failure scores 0 without
    a verdict (design, "Actor failures").
    """

    by_case = {case.case_id: case for case in cases}
    secret = _secret(seed, inputs_digest)
    answered = sorted((s for s in sessions if s["outcome"] == "ok"), key=lambda s: s["session_id"])
    originals: list[dict] = []
    arm_key: dict[str, dict] = {}
    for session in answered:
        item_id = _opaque(secret, session["session_id"])
        originals.append(
            item_payload(
                by_case[session["case_id"]], session["answer"], item_id, tool_names=tool_names
            )
        )
        arm_key[item_id] = {
            "session_id": session["session_id"],
            "case_id": session["case_id"],
            "arm": session["arm"],
            "repeat": session["repeat"],
            "rejudge_of": None,
        }
    count = math.ceil(protocol.REJUDGE_FRACTION * len(originals))
    rejudged: list[dict] = []
    for index in deterministic_permutation(len(originals), f"{secret}:rejudge")[:count]:
        original = originals[index]
        session_id = arm_key[original["item_id"]]["session_id"]
        fresh = _opaque(secret, "rejudge", session_id)
        rejudged.append({**original, "item_id": fresh})
        arm_key[fresh] = {**arm_key[original["item_id"]], "rejudge_of": original["item_id"]}
    items = tuple(shuffled(originals + rejudged, f"{secret}:order"))
    labels = {case.case_id: case_labels(case) for case in cases}
    return ItemSet(items, arm_key, labels)


def write_items(layout: Layout, item_set: ItemSet) -> None:
    layout.judge.mkdir(parents=True, exist_ok=True)
    layout.unblinding.mkdir(parents=True, exist_ok=True)
    (layout.judge / "items.jsonl").write_text(
        "".join(
            json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n" for item in item_set.items
        ),
        encoding="utf-8",
    )
    (layout.unblinding / "arm-key.json").write_text(
        json.dumps(item_set.arm_key, indent=2, sort_keys=True), encoding="utf-8"
    )
    (layout.unblinding / "labels.json").write_text(
        json.dumps(item_set.labels, indent=2, sort_keys=True), encoding="utf-8"
    )


def read_items(layout: Layout) -> list[dict]:
    path = layout.judge / "items.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# --------------------------------------------------------------------------
# Running a judge.
# --------------------------------------------------------------------------


def judge_prompt(item: Mapping, *, text: str = "judge.md") -> str:
    """One item's prompt: the instructions in ``texts/<text>``, then the item."""

    instructions = (TEXTS / text).read_text(encoding="utf-8").strip()
    return (
        instructions + "\n\nThe item:\n\n" + json.dumps(item, indent=2, ensure_ascii=False) + "\n"
    )


def codex_argv(
    *, model: str, effort: str, schema: Path, out: Path, sandbox: Path, prompt: str
) -> list[str]:
    """``codex exec`` with no user config, rules, hooks, plugins, project docs,
    skill list or tools, read-only, in an empty directory; stdin is closed by
    the caller.

    A read-only shell still reads the whole disk, the arm key included, so the
    shell, its exec successor and the code-mode host are disabled by their
    feature names. The global ``AGENTS.md`` under the Codex home still reaches
    the judge: no flag of this version drops it, and it is the same text for
    every item (a named limitation of the route).
    """

    return [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
        "hooks",
        "--disable",
        "plugins",
        "--disable",
        "apps",
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
        "--disable",
        "code_mode_host",
        "-s",
        "read-only",
        "-C",
        str(sandbox),
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "skills.include_instructions=false",
        "--output-schema",
        str(schema),
        "-o",
        str(out),
        "--json",
        prompt,
    ]


def claude_argv(
    *,
    executable: str,
    model: str,
    effort: str | None,
    schema: Mapping,
    prompt: str,
    mcp_config: Path,
) -> list[str]:
    """The other family's judge: no tools, no MCP server, no persisted session."""

    return [
        executable,
        "-p",
        prompt,
        "--model",
        model,
        *(("--effort", effort) if effort else ()),
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--setting-sources",
        "project",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
    ]


def validate_verdict(item: Mapping, verdict: object) -> dict:
    """The verdict, checked against the item it answers; raises on any mismatch."""

    if not isinstance(verdict, dict) or verdict.get("item_id") != item["item_id"]:
        raise JudgeError("the verdict does not name its item")
    for kind, allowed in (
        ("required", protocol.REQUIRED_VERDICTS),
        ("forbidden", protocol.FORBIDDEN_VERDICTS),
    ):
        expected = sorted(entry["label"] for entry in item[kind])
        given = verdict.get(kind) or []
        if sorted(entry.get("label") for entry in given) != expected:
            raise JudgeError(f"the verdict's {kind} labels are not the item's")
        if any(entry.get("verdict") not in allowed for entry in given):
            raise JudgeError(f"a {kind} verdict is outside the schema")
    no_memory = verdict.get("no_memory") or {}
    if not item["expects_memory"] and not isinstance(
        no_memory.get("asserts_unsupplied_user_facts"), bool
    ):
        raise JudgeError("a no-memory item needs the contamination verdict")
    span = no_memory.get("span")
    if (
        not item["expects_memory"]
        and no_memory["asserts_unsupplied_user_facts"]
        and not (isinstance(span, str) and span.strip())
    ):
        raise JudgeError("a contamination verdict needs the span that shows it")
    return {
        "item_id": verdict["item_id"],
        "required": {e["label"]: e["verdict"] for e in verdict["required"]},
        "forbidden": {e["label"]: e["verdict"] for e in verdict["forbidden"]},
        "contaminated": bool(no_memory.get("asserts_unsupplied_user_facts"))
        if not item["expects_memory"]
        else None,
        "span": no_memory.get("span") if not item["expects_memory"] else None,
    }


#: The ``codex exec --json`` item types a judge with no tools emits: its
#: messages, its reasoning and the CLI's own notices. Any other type is a tool.
_JUDGE_ITEM_TYPES = frozenset({"agent_message", "reasoning", "error"})


def _refuse_tool_use(stdout: str) -> None:
    """Refuse a verdict whose judge ran a tool: a tool could read the arm key."""

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") not in _JUDGE_ITEM_TYPES:
            raise JudgeError(f"the judge ran a tool ({item.get('type')}); its verdict is not blind")


def run_structured(
    items: Sequence[Mapping],
    *,
    route: str,
    model: str,
    effort: str | None,
    schema: Mapping,
    prompt_for: Callable[[Mapping], str],
    validate: Callable[[Mapping, object], dict],
    out_dir: Path,
    sandbox: Path,
    claude: str = "claude",
    launcher: Sequence[str] = (),
    on_answer: Callable[[], None] = lambda: None,
) -> int:
    """Answer every item not yet answered in ``out_dir``, one headless call per
    item through ``route``'s structured output; resumable item by item.

    A failed item is retried, then the stage stops with the item named: a
    spent quota and a broken route look alike from here, and both resume
    by running the stage again. ``on_answer`` runs once an item has its
    stored answer, from this run or an earlier one. Returns the items
    answered now.
    """

    if route not in ROUTES:
        raise JudgeError(f"unknown judge route {route!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    sandbox.mkdir(parents=True, exist_ok=True)
    schema_path = out_dir.with_name(f"{out_dir.name}-schema.json")
    schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    empty_mcp = sandbox.parent / "no-mcp.json"
    empty_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    log_path = out_dir.with_name(f"{out_dir.name}-attempts.jsonl")
    # The launching agent session's variables would make the call a nested
    # child of that session; it starts as a plain headless run instead.
    env = {**environment_floor(os.environ)[0], **CLIENT_PINS}
    answered = 0
    for item in items:
        target = out_dir / f"{item['item_id']}.json"
        if target.exists():
            on_answer()
            continue
        prompt = prompt_for(item)
        if len(prompt.encode("utf-8")) > _PROMPT_MAX_BYTES:
            raise JudgeError(
                f"item {item['item_id']}'s prompt exceeds one argv string; it cannot be judged as built"
            )
        last_error = ""
        for attempt in range(1, _ITEM_ATTEMPTS + 1):
            raw_path = out_dir / f"{item['item_id']}.raw-{attempt}"
            if route == "codex":
                argv = codex_argv(
                    model=model,
                    effort=effort or "",
                    schema=schema_path,
                    out=raw_path,
                    sandbox=sandbox,
                    prompt=prompt,
                )
            else:
                argv = launched_argv(
                    launcher,
                    claude_argv(
                        executable=claude,
                        model=model,
                        effort=effort,
                        schema=schema,
                        prompt=prompt,
                        mcp_config=empty_mcp,
                    ),
                )
            completed = subprocess.run(  # noqa: S603 - fixed judge argv
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=sandbox,
                env=env,
                timeout=1800,
            )
            try:
                if completed.returncode != 0:
                    raise JudgeError(
                        f"exit {completed.returncode}: {completed.stderr.strip()[-300:]}"
                    )
                if route == "codex":
                    events = raw_path.with_name(f"{item['item_id']}.events-{attempt}.jsonl")
                    events.write_text(completed.stdout, encoding="utf-8")
                    _refuse_tool_use(completed.stdout)
                    payload = json.loads(raw_path.read_text(encoding="utf-8"))
                else:
                    raw_path.write_text(completed.stdout, encoding="utf-8")
                    payload = json.loads(completed.stdout).get("structured_output")
                answer = validate(item, payload)
            except (JudgeError, OSError, json.JSONDecodeError, AttributeError) as error:
                last_error = str(error)
                with open(log_path, "a", encoding="utf-8") as log:
                    log.write(
                        json.dumps(
                            {"item_id": item["item_id"], "attempt": attempt, "error": last_error}
                        )
                        + "\n"
                    )
                continue
            target.write_text(json.dumps(answer, indent=2, sort_keys=True), encoding="utf-8")
            answered += 1
            on_answer()
            break
        else:
            raise JudgeError(
                f"item {item['item_id']} failed {_ITEM_ATTEMPTS} times on route {route}: {last_error}"
            )
    return answered


def run_judge(
    layout: Layout,
    *,
    route: str,
    model: str,
    effort: str | None,
    claude: str = "claude",
    launcher: Sequence[str] = (),
    items: Sequence[Mapping] | None = None,
    out_dir: Path | None = None,
) -> dict:
    """Judge every item not yet judged on ``route``: the run's items into
    ``judge/verdicts/<route>``, or ``items`` into ``out_dir``. One judge judges
    every item a route judges, so the route's manifest refuses another."""

    if route not in ROUTES:
        raise JudgeError(f"unknown judge route {route!r}")
    first_pass = out_dir is None
    items = read_items(layout) if items is None else list(items)
    out_dir = out_dir or layout.judge / "verdicts" / route
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    manifest = out_dir.with_name(f"{out_dir.name}-judge.json")
    record = {
        "route": route,
        "model": model,
        "effort": effort,
        "limitations": list(ROUTE_LIMITATIONS[route]),
    }
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != record:
        raise JudgeError(
            f"route {route} was started with another judge; one judge judges every item"
        )
    manifest.write_text(json.dumps(record, indent=2), encoding="utf-8")
    judged = run_structured(
        items,
        route=route,
        model=model,
        effort=effort,
        schema=judge_schema(),
        prompt_for=judge_prompt,
        validate=validate_verdict,
        out_dir=out_dir,
        sandbox=layout.judge / "sandbox",
        claude=claude,
        launcher=launcher,
        # A route spent before its first verdict leaves no record, so the
        # fallback route becomes the route that judged first.
        on_answer=(lambda: record_first_route(layout, route)) if first_pass else lambda: None,
    )
    return {"route": route, "judged_now": judged, "items": len(items)}


def record_first_route(layout: Layout, route: str) -> None:
    """Record the first route to give a verdict on the run's items, once: it
    stands in every scoring, and its agreement decides whether the recovery
    route must stand with it."""

    path = layout.judge / "first-route.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"route": route}), encoding="utf-8")


def first_route(layout: Layout) -> str:
    path = layout.judge / "first-route.json"
    if not path.exists():
        raise UnblindingError("no record of the route that judged first")
    return json.loads(path.read_text(encoding="utf-8"))["route"]


def judge_manifest(layout: Layout, route: str) -> dict:
    """The judge a route's first pass ran with: its model and effort."""

    path = layout.judge / "verdicts" / f"{route}-judge.json"
    if not path.exists():
        raise UnblindingError(f"route {route} has no judge manifest")
    return json.loads(path.read_text(encoding="utf-8"))


def load_answers(out_dir: Path) -> dict[str, dict]:
    """Every stored answer in one route's directory, by item id."""

    if not out_dir.exists():
        return {}
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8")) for path in out_dir.glob("*.json")
    }


def load_verdicts(layout: Layout, route: str) -> dict[str, dict]:
    return load_answers(layout.judge / "verdicts" / route)


def agreement(verdicts: Mapping[str, Mapping], pairs: Sequence[tuple[str, str]]) -> dict:
    """Agreement per verdict kind between items judged twice by one judge."""

    totals = {"required": [0, 0], "forbidden": [0, 0], "contamination": [0, 0]}
    for first_id, second_id in pairs:
        first, second = verdicts.get(first_id), verdicts.get(second_id)
        if first is None or second is None:
            continue
        for kind in ("required", "forbidden"):
            for label, value in first[kind].items():
                totals[kind][0] += value == second[kind].get(label)
                totals[kind][1] += 1
        if first["contaminated"] is not None:
            totals["contamination"][0] += first["contaminated"] == second["contaminated"]
            totals["contamination"][1] += 1
    report = {
        kind: {"agree": agree, "of": of, "rate": (agree / of) if of else None}
        for kind, (agree, of) in totals.items()
    }
    rate = report["required"]["rate"]
    report["recovery_required"] = rate is not None and rate < protocol.REQUIRED_AGREEMENT_FLOOR
    report["pairs"] = len(pairs)
    return report


def rejudge_pairs(arm_key: Mapping[str, Mapping]) -> list[tuple[str, str]]:
    return sorted(
        (entry["rejudge_of"], item_id) for item_id, entry in arm_key.items() if entry["rejudge_of"]
    )


def judge_agreement(layout: Layout, route: str) -> dict:
    """Agreement for one route. Reads only the item pairs, never which arm they are."""

    arm_key = json.loads((layout.unblinding / "arm-key.json").read_text(encoding="utf-8"))
    report = agreement(load_verdicts(layout, route), rejudge_pairs(arm_key))
    (layout.judge / f"{route}-agreement.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


# --------------------------------------------------------------------------
# Unblinding and the tie-break.
# --------------------------------------------------------------------------

#: Order from best to worst for the candidate, per verdict kind. The tie-break
#: keeps the worse verdict for M and the better one for A and P, which is the
#: verdict that weighs against the switch (design, "Judge noise").
_REQUIRED_ORDER = {"used": 0, "missing": 1, "contradicted": 2}
_FORBIDDEN_ORDER = {"absent": 0, "mentioned_as_past": 1, "asserted_as_current": 2}
CANDIDATE_ARM = "M"


@dataclass(frozen=True)
class Unblinded:
    """The arm key, checked against every item id after every verdict is in.

    The only way to reach the tie-break: it takes an arm, and an arm comes
    from here.
    """

    arm_key: Mapping[str, Mapping]
    routes: tuple[str, ...]


def unblind(layout: Layout, *, routes: Sequence[str], expected: Mapping[str, Mapping]) -> Unblinded:
    """The stored arm key, checked entry by entry against ``expected``: the key
    rebuilt from the frozen plan and the captured sessions (``build_items``)."""

    items = read_items(layout)
    key_path = layout.unblinding / "arm-key.json"
    if not key_path.exists():
        raise UnblindingError("no arm key: the items were not built by this harness")
    arm_key = json.loads(key_path.read_text(encoding="utf-8"))
    ids = {item["item_id"] for item in items}
    if set(arm_key) != ids:
        raise UnblindingError(
            f"the arm key names {len(set(arm_key) - ids)} ids no item has and misses {len(ids - set(arm_key))} item ids"
        )
    rebuilt = json.loads(json.dumps(expected))
    differing = [i for i in set(arm_key) | set(rebuilt) if arm_key.get(i) != rebuilt.get(i)]
    if differing:
        raise UnblindingError(
            f"the arm key differs from the key rebuilt from the plan and the captured sessions "
            f"for {len(differing)} items"
        )
    if not routes or len(set(routes)) != len(routes) or set(routes) - set(ROUTES):
        raise UnblindingError(f"the standing routes must be distinct routes of {ROUTES}")
    for route in routes:
        missing = ids - set(load_verdicts(layout, route))
        if missing:
            raise UnblindingError(
                f"route {route} has not judged {len(missing)} items; judging is not complete"
            )
    # Design, "Judge noise": the route that judged first always stands, and
    # its low required-item agreement makes the other family's judge part of
    # every standing verdict through the tie-break.
    judged_first = first_route(layout)
    if judged_first not in routes:
        raise UnblindingError(
            f"route {judged_first} judged first, so its verdicts must stand; routes {list(routes)} drop it"
        )
    first = agreement(load_verdicts(layout, judged_first), rejudge_pairs(arm_key))
    if first["recovery_required"] and len(routes) < 2:
        raise UnblindingError(
            f"route {judged_first}'s required-item agreement is {first['required']['rate']:.2f}, "
            f"below {protocol.REQUIRED_AGREEMENT_FLOOR}: the recovery route must judge every item "
            "and stand with it"
        )
    unblinded = Unblinded(arm_key=arm_key, routes=tuple(routes))
    (layout.unblinding / "unblinded.json").write_text(
        json.dumps({"routes": list(routes), "items": len(ids)}, indent=2), encoding="utf-8"
    )
    return unblinded


def tie_break(first: Mapping, second: Mapping, *, unblinded: Unblinded) -> dict:
    """One item's verdict from two judges: agreement stands, else the verdict
    that weighs against the switch."""

    if not isinstance(unblinded, Unblinded):
        raise UnblindingError("the tie-break runs only after unblinding")
    arm = unblinded.arm_key[first["item_id"]]["arm"]
    pick = max if arm == CANDIDATE_ARM else min
    resolved = {"item_id": first["item_id"], "required": {}, "forbidden": {}}
    for kind, order in (("required", _REQUIRED_ORDER), ("forbidden", _FORBIDDEN_ORDER)):
        for label, value in first[kind].items():
            other = second[kind][label]
            resolved[kind][label] = (
                value if value == other else pick((value, other), key=order.__getitem__)
            )
    if first["contaminated"] is None:
        resolved["contaminated"] = None
    elif first["contaminated"] == second["contaminated"]:
        resolved["contaminated"] = first["contaminated"]
    else:
        resolved["contaminated"] = arm == CANDIDATE_ARM
    return resolved


def resolved_verdicts(layout: Layout, unblinded: Unblinded) -> dict[str, dict]:
    """Each original item's standing verdict, keyed by session id.

    One route: its first-pass verdict. Two routes: the tie-break of the two
    first-pass verdicts. Re-judged copies feed agreement only.
    """

    by_route = [load_verdicts(layout, route) for route in unblinded.routes]
    resolved: dict[str, dict] = {}
    for item_id, entry in unblinded.arm_key.items():
        if entry["rejudge_of"]:
            continue
        verdict = by_route[0][item_id]
        if len(by_route) == 2:
            second = {**by_route[1][item_id], "item_id": item_id}
            verdict = tie_break({**verdict, "item_id": item_id}, second, unblinded=unblinded)
        resolved[entry["session_id"]] = verdict
    return resolved
