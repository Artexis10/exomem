"""The `session-close` suite: a short session in a real seeded vault, ending in a close, run on one arm.

The scripted harness cannot express this: it serves recorded replies to one prompt. Here the arm's own server (a real
stdio `exomem`, `PYTHONPATH=<arm>/src`) runs on a small seeded vault, and one `claude -p --input-format stream-json`
process takes the case's user turns one after another, so the server and its resolver cache live across the session.
A run ends with a snapshot diff of the vault: which seeded pages changed, what the recap is, and what it links.

A run holds raw observations only and judges nothing. `session_score.py` reads the answer key and the judge, so the
measured session never writes or sees the key.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO

from epistemic.journeys.f27_replay import (
    REPO_ROOT,
    count_hook_activity,
    discover_agent_envelope,
    environment_floor,
    fault_reason,
    refuse_unsafe_out_dir,
)
from epistemic.journeys.f33_referent_replay import _lines as stream_events
from epistemic.journeys.f33_referent_replay import tool_calls
from membench.trackc.witness_join import parse_stream_json_transcript

from exomem import memory_refs, note_types, prominence, source_closure

from . import checks
from .recording import Arm, checkout_sha, worker_env
from .scripted_replies import SCENARIO_DIR

SUITE = "session-close"
# Client modes are a closed harness protocol, not vault vocabulary.
CLIENT_MODES = ("plugin", "tool-only")
DEFAULT_CLIENT_MODE = CLIENT_MODES[0]
SEED_WORKER = Path(__file__).resolve().parent / "session_seed.py"
#: The engagement levels, read from the product so the list lives in one place; a typo would
#: silently read as balanced.
ENGAGEMENTS = prominence.CANON
#: A fresh server answers `RETRIEVAL_INDEX_WARMING` for about ten seconds; the first turn waits for the index.
WARM_HOLD_SECONDS = 15.0
TURN_TIMEOUT_SECONDS = 300.0
EXIT_GRACE_SECONDS = 30.0
#: The product's reserved folder for a recorded recap (`Sources/Episodes/`): a closed set the product fixes.
RECAP_FOLDER = "Sources/Episodes/"
#: Files the server maintains itself; a change to them is not a page the agent wrote.
GENERATED_PAGES = ("index.md", "log.md")
WARMING_MARK = "RETRIEVAL_INDEX_WARMING"
HOOK_EVENTS = ("Stop", "UserPromptSubmit")
SKILL_FILES = ("SKILL.md", "references/engagement.md")


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    seed: Path
    engagement: str
    turns: tuple[str, ...]
    path: Path
    recall_turn: str | None = None


@dataclass(frozen=True)
class CreditsRun:
    account: str
    budget_usd: float
    reason: str
    executable: Path | None = None

    def __post_init__(self) -> None:
        if not self.account.strip() or not self.reason.strip():
            raise ValueError("--credits-account and --credits-reason must not be empty")
        if not math.isfinite(self.budget_usd) or self.budget_usd <= 0:
            raise ValueError("--credits-budget-usd must be positive and finite")


def transport_metadata(credits: CreditsRun | None) -> dict:
    # Transport names are a closed harness protocol; account values stay caller data.
    return {
        "transport": "claude-credits" if credits else "native",
        "credits_account": credits.account if credits else None,
        "credits_budget_usd": credits.budget_usd if credits else None,
    }


def suite_dir() -> Path:
    return SCENARIO_DIR / SUITE


def load_case(path: Path) -> Case:
    """One case file. `session_of` takes the seed and the turns of another case, so two cases differ in one field."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    inherited = raw.get("session_of")
    base = (
        json.loads((path.parent / f"{inherited}.json").read_text(encoding="utf-8"))
        if inherited
        else raw
    )
    problems = []
    if raw.get("suite") != SUITE:
        problems.append(f"suite must be {SUITE!r}")
    if raw.get("engagement") not in ENGAGEMENTS:
        problems.append(f"engagement must be one of {ENGAGEMENTS}")
    turns = base.get("turns")
    if not (isinstance(turns, list) and turns and all(isinstance(t, str) and t.strip() for t in turns)):
        problems.append("turns must be a non-empty list of strings")
    seed = suite_dir() / "seeds" / str(base.get("seed"))
    if not seed.is_dir():
        problems.append(f"seed {base.get('seed')!r} is not a folder under seeds/")
    if "answer_key" in raw or "answer_key" in base:
        problems.append("answer_key belongs in the scorer-only answer-keys folder")
    recall_turn = raw.get("recall_turn")
    if recall_turn is not None and not (isinstance(recall_turn, str) and recall_turn.strip()):
        problems.append("recall_turn must be a non-empty string when supplied")
    if problems:
        raise SystemExit(f"{path.name}: " + "; ".join(problems))
    return Case(
        raw["id"], raw["title"], seed, raw["engagement"], tuple(turns), path, recall_turn
    )


def load_cases(selector: str, case_dir: Path | None = None) -> list[Case]:
    cases = [load_case(p) for p in sorted((case_dir or suite_dir()).glob("*.json"))]
    chosen = cases if selector == "all" else [c for c in cases if c.id == selector or c.id.startswith(selector)]
    if not chosen:
        raise SystemExit(f"no case matches {selector!r}: {[c.id for c in cases]}")
    return chosen


# --------------------------------------------------------------------------
# The vault: seeding, snapshot, diff.
# --------------------------------------------------------------------------


def pinned_env(arm: Arm, work: Path, case: Case) -> dict[str, str]:
    """The arm's interpreter path and a state tree under `work`: a real vault or service state is never reachable."""
    state = work / "state"
    return worker_env(arm, work / "scratch") | {
        "EXOMEM_VAULT_PATH": str(work / "vault"),
        "EXOMEM_STATE_ROOT": str(state),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state / "lease"),
        "EXOMEM_CONFIG_PATH": str(work / "exomem-config.json"),
        "EXOMEM_LOG_DIR": str(work / "logs"),
        "EXOMEM_PROMINENCE": case.engagement,
        "EXOMEM_DISABLE_EMBEDDINGS": "1",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_QUERY_LOG": "1",
    }


def seed_vault(case: Case, arm: Arm, work: Path, env: dict[str, str]) -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(SEED_WORKER), str(case.seed), str(work / "vault"), str(arm.root)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise RuntimeError(f"seeding failed: {(done.stderr or done.stdout).strip()[-400:]}")


def knowledge_base(vault: Path) -> Path:
    from exomem.kbdir import kb_dirname

    return vault / kb_dirname()


def snapshot(vault: Path) -> dict[str, dict]:
    """Every page of the knowledge base: path (relative to it) to its text and last write. The top-level folders that
    start with an underscore hold the vault's own schema and are not pages."""
    root = knowledge_base(vault)
    pages: dict[str, dict] = {}
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if rel.parts[0].startswith("_") or rel.name in GENERATED_PAGES:
            continue
        pages[rel.as_posix()] = {
            "text": path.read_text(encoding="utf-8"),
            "mtime_ns": path.stat().st_mtime_ns,
        }
    return pages


def page_title(text: str) -> str:
    from exomem.vault import parse_frontmatter

    _meta, body, _raw = parse_frontmatter(text)
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def resolve_link(target: str, pages: dict[str, dict]) -> list[str]:
    """The pages a wikilink target names: by path, then file name, then title, comparing case-folded strings.
    One page is a resolved link, none is a dangling one, several is ambiguous."""
    from exomem.kbdir import kb_prefix
    from exomem.vault import split_wikilink_target

    page, _anchor = split_wikilink_target(target)
    wanted = page.removeprefix(kb_prefix()).removesuffix(".md").casefold()
    for names in (
        lambda p: p.removesuffix(".md"),
        lambda p: Path(p).stem,
        lambda p: page_title(pages[p]["text"]),
    ):
        hits = sorted(p for p in pages if names(p).casefold() == wanted)
        if hits:
            return hits
    return []


def unified(before: str, after: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"before/{name}",
            f"after/{name}",
        )
    )


def diff_vault(before: dict[str, dict], after: dict[str, dict]) -> dict:
    """Raw observations of what the session left: seeded pages changed or removed, pages created, the recaps among
    them, and for each changed page its note type's role and the recaps it cites. Nothing here is judged."""
    changed = {
        p: unified(before[p]["text"], after[p]["text"], p)
        for p in before
        if p in after and before[p]["text"] != after[p]["text"]
    }
    removed = sorted(p for p in before if p not in after)
    created = {p: after[p]["text"] for p in sorted(after) if p not in before}
    recaps = {path: {"text": text} for path, text in created.items() if path.startswith(RECAP_FOLDER)}
    return {
        "changed": changed,
        "removed": removed,
        "created": created,
        "recaps": recaps,
        "pages": {p: changed_page(after[p]["text"], after, recaps) for p in changed},
        "order": write_order(changed, recaps, after),
    }


def changed_page(text: str, after: dict[str, dict], recaps: dict) -> dict:
    """Whether a changed seeded page is of a compiled note type, and which written recaps its `sources:` claim names.
    A claim names a recap by memory ref, or by path, file name or title as a wikilink does."""
    from exomem.vault import parse_frontmatter

    meta, _body, _raw = parse_frontmatter(text)
    # The shipped pack alone: the seeds use shipped types only, and an arm adds no type of its own.
    basis = note_types.Basis(None)
    refs = {
        memory_refs.memory_ref(id_): path
        for path in recaps
        if (id_ := memory_refs.normalize_id(parse_frontmatter(after[path]["text"])[0].get("exomem_id")))
    }
    cited = set()
    for claim in source_closure.claims_in(meta, basis):
        hits = [refs[claim.lower()]] if claim.lower() in refs else resolve_link(claim, after)
        cited.update(path for path in hits if path in recaps)
    return {"compiled": basis.selects(meta.get("type"), note_types.compiled), "cites": sorted(cited)}


def write_order(changed: dict, recaps: dict, after: dict[str, dict]) -> str:
    """Where the changed pages' last write falls against the first recap's write: `before` (or at the same time),
    `after`, `mixed`, `no_change` or `no_recap`. The last write of a page is all a file keeps."""
    if not changed:
        return "no_change"
    if not recaps:
        return "no_recap"
    recap_at = min(after[p]["mtime_ns"] for p in recaps)
    early = [after[p]["mtime_ns"] <= recap_at for p in changed]
    return "before" if all(early) else "after" if not any(early) else "mixed"


# --------------------------------------------------------------------------
# The agent: one process, one user turn at a time.
# --------------------------------------------------------------------------


def stage_agent_files(arm: Arm, work: Path, env: dict[str, str], client_mode: str = DEFAULT_CLIENT_MODE) -> dict[str, Path]:
    """Stage the same real MCP server, with plugin guidance or an explicit request to disable hooks."""
    mcp = work / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {checks.SERVER: {
        "command": sys.executable, "args": ["-m", "exomem", "--transport", "stdio"], "env": env,
    }}}), encoding="utf-8")
    files = {"mcp": mcp, "settings": work / "settings.json"}
    settings = {"disableAllHooks": True}
    if client_mode == DEFAULT_CLIENT_MODE:
        schema = arm.root / "src" / "exomem" / "_scaffold" / "_Schema"
        files["skill"] = work / "skill.md"
        files["skill"].write_text("\n\n".join((schema / name).read_text(encoding="utf-8") for name in SKILL_FILES), encoding="utf-8")
        plugin = arm.root / "plugins" / "claude-code"
        declared = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        settings = {"hooks": json.loads(json.dumps({e: declared[e] for e in HOOK_EVENTS}).replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)))}
    files["settings"].write_text(json.dumps(settings), encoding="utf-8")
    return files


def agent_argv(executable: Path, files: dict[str, Path], model: str, credits: CreditsRun | None = None) -> list[str]:
    shared = [
        "--include-hook-events",
        "--strict-mcp-config",
        "--mcp-config",
        str(files["mcp"]),
        "--settings",
        str(files["settings"]),
        "--setting-sources",
        "project",
        "--allowedTools",
        f"mcp__{checks.SERVER}",
        "--tools",
        "",
        "--max-turns",
        "40",
    ] + (["--append-system-prompt-file", str(files["skill"])] if "skill" in files else [])
    if credits:
        # The maintained duplex protocol owns print, framing, model and session flags before `--`.
        return [str(executable), "claude-credits", "run", "--account", credits.account,
                "--model", model, "--budget", str(credits.budget_usd), "--reason", credits.reason,
                "--stream-json", "--", *shared]
    return [str(executable), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            *shared, "--model", model, "--no-session-persistence"]


def agent_env(work: Path, case: Case) -> dict[str, str]:
    """The f27 floor (HOME kept for the login) without the parent's `EXOMEM_*`, then the pins the hooks read: they
    keep their state under `work` and never reach a real vault or service."""
    floor, _removed = environment_floor(os.environ)
    floor = {k: v for k, v in floor.items() if not k.startswith("EXOMEM_")}
    return floor | {
        "EXOMEM_PROMINENCE": case.engagement,
        "EXOMEM_HOOK_HOME": str(work / "hook-home"),
        "EXOMEM_HOOK_CLIENT": "claude",
        "EXOMEM_RETRIEVE_INJECT": "mcp",
        "EXOMEM_CONFIG_PATH": str(work / "exomem-config.json"),
    }


def user_message(text: str) -> str:
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    )


def drive_session(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    turns: tuple[str, ...],
    stream_path: Path,
    hook_turn: Callable[[str], str] | None = None,
) -> SimpleNamespace:
    """Start the agent, hold the first turn for the index warm, then send each turn after the previous one's `result`
    event. With `hook_turn`, one more turn follows the last: its text is built from the session id of the stream.
    Returns the whole stream, the exit code and why it stopped early, if it did."""
    lines: queue.Queue[str | None] = queue.Queue()
    proc = subprocess.Popen(  # noqa: S603
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )

    def read_stdout() -> None:
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    stderr: list[str] = []
    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()
    stream, finished, eof_seen = [], 0, False
    time.sleep(WARM_HOLD_SECONDS)

    def exchange(number: int, turn: str, sink: TextIO) -> str | None:
        """One user turn and the agent's answer to it; the reason the session stopped, or None."""
        nonlocal finished, eof_seen
        try:
            proc.stdin.write(user_message(turn) + "\n")
            proc.stdin.flush()
        except OSError:
            return f"the agent closed its input before turn {number}"
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while True:
            try:
                raw = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                return f"turn {number} timed out after {TURN_TIMEOUT_SECONDS:.0f} s"
            if raw is None:
                eof_seen = True
                return f"the agent exited during turn {number}"
            stream.append(raw)
            sink.write(raw)
            sink.flush()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                finished += 1
                return None

    stopped, hook_text = None, None
    with stream_path.open("w", encoding="utf-8") as sink:
        for number, turn in enumerate(turns, start=1):
            stopped = exchange(number, turn, sink)
            if stopped:
                break
        if not stopped and hook_turn is not None:
            hook_text = hook_turn(str(init_field("".join(stream), "session_id")))
            stopped = exchange(len(turns) + 1, hook_text, sink)
        try:
            proc.stdin.close()
        except OSError:
            pass
        exit_deadline = time.monotonic() + EXIT_GRACE_SECONDS
        # SessionEnd hooks can run after stdin closes; a result event is not stdout EOF.
        while not eof_seen and time.monotonic() < exit_deadline:
            try:
                raw = lines.get(timeout=max(0, exit_deadline - time.monotonic()))
            except queue.Empty:
                break
            if raw is None:
                eof_seen = True
            else:
                stream.append(raw)
                sink.write(raw)
                sink.flush()
        if not eof_seen:
            stopped = stopped or "agent stdout observation is incomplete: EOF was not observed before the exit deadline"
    if os.name == "posix":
        def wait_without_reaping(deadline: float) -> None:
            while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, EXIT_GRACE_SECONDS)
                time.sleep(min(0.05, remaining))

        try:
            try:
                wait_without_reaping(exit_deadline)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                wait_without_reaping(time.monotonic() + EXIT_GRACE_SECONDS)
        finally:
            # The unreaped leader reserves our group ID until the remaining stdio server is killed.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=EXIT_GRACE_SECONDS)
    else:
        try:
            proc.wait(timeout=max(0, exit_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=EXIT_GRACE_SECONDS)
    return SimpleNamespace(
        stdout="".join(stream),
        stderr="".join(stderr),
        returncode=proc.returncode,
        stdout_eof=eof_seen,
        stopped=stopped,
        finished=finished,
        hook_turn=hook_text,
    )


def hook_ask(arm: Arm, session_id: str) -> str:
    """The arm's own episode ask for this session, read from the arm's hook script: the Stop hook sends it in live
    closes, so no copy lives here. The key is the one the hook derives from the client and the session id."""
    path = arm.root / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py"
    spec = importlib.util.spec_from_file_location(f"episode_ask_{arm.label}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EPISODE_ASK.replace("{key}", module.episode_key("claude", session_id))


def tool_trail(stdout: str) -> dict:
    """The tool calls in order, each with its reply, read from the stream. `compile` holds the `compile` block of each
    `record_episode` reply, and `warming` counts the replies that said the index was still warming."""
    calls = tool_calls(stdout)
    compile_blocks: list[object] = []
    warming = 0
    for call in calls:
        warming += WARMING_MARK in json.dumps(call["result"])
        if str(call["name"]).endswith("record_episode"):
            compile_blocks.append(compile_block_of(call["result"]))
    return {"calls": calls, "compile": compile_blocks, "warming": warming}


def compile_block_of(content: object) -> object:
    """The reply's `compile` block (None when the reply has none), found by its key in the parsed JSON text."""
    for part in content if isinstance(content, list) else [content]:
        text = part.get("text") if isinstance(part, dict) else part
        try:
            payload = json.loads(text) if isinstance(text, str) else None
        except json.JSONDecodeError:
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "compile" in node:
                    return node["compile"]
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return None


def replies_by_turn(stdout: str) -> list[str]:
    """What the agent said to the person after each user turn: the `result` text of each turn."""
    replies = []
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            replies.append(event.get("result") if isinstance(event.get("result"), str) else "")
    return replies


# --------------------------------------------------------------------------
# One run, and the suite.
# --------------------------------------------------------------------------


def client_mode_error(client_mode: str, result: dict) -> str | None:
    """A no-hook measurement needs a complete connected trace; contamination preserves evidence as unknown."""
    error = result.get("error")
    if error is not None or client_mode != "tool-only":
        return error
    count = result.get("hook_invocations")
    starts = result.get("hook_starts")
    if type(count) is not int or count < 0 or type(starts) is not int or starts < 0 or (result.get("server_connected") or {}).get(checks.SERVER) != "connected":
        return "tool-only hook observations are unavailable; no-hook acceptance is unknown"
    if count or starts:
        return f"tool-only observed {count} completed hook invocation(s) and {starts} hook start(s); no-hook acceptance is unknown"
    return None


def session_result(done: SimpleNamespace, expected_turns: int, client_mode: str = DEFAULT_CLIENT_MODE) -> dict:
    """Raw evidence from a finished process, without scoring its answer or its tool choices."""
    transcript = parse_stream_json_transcript(done.stdout.splitlines())
    trail = tool_trail(done.stdout)
    connected = server_state(done.stdout)
    error = (
        done.stopped
        or fault_reason(done, transcript)
        or (None if done.finished == expected_turns else "a turn returned no result")
        or (None if connected.get(checks.SERVER) == "connected" else "the MCP server did not connect")
    )
    invocations, firings = count_hook_activity(done.stdout) if done.stdout_eof else (None, None)
    result = {
        "client_mode": client_mode,
        "error": error,
        "stdout_eof": done.stdout_eof,
        "hook_invocations": invocations,
        # These are closed Claude wire subtypes; an unfinished hook_started still proves execution.
        "hook_starts": sum(event.get("type") == "system" and event.get("subtype") == "hook_started" for event in stream_events(done.stdout)) if done.stdout_eof else None,
        "capture_nudges": firings,
        "session_id": init_field(done.stdout, "session_id"),
        "actor_version": init_field(done.stdout, "claude_code_version"),
        "replies": replies_by_turn(done.stdout),
        "calls": trail["calls"],
        "compile_blocks": trail["compile"],
        "warming_replies": trail["warming"],
        "usage": transcript.usage,
        "cost_usd": transcript.total_cost_usd,
        "api_key_source": init_field(done.stdout, "apiKeySource"),
        "server_connected": connected,
        "stderr": done.stderr,
        "returncode": done.returncode,
    }
    result["error"] = client_mode_error(client_mode, result)
    return result


def fresh_recall(case: Case, arm: Arm, work: Path, *, model: str, executable: Path, env: dict[str, str], client_mode: str = DEFAULT_CLIENT_MODE, credits: CreditsRun | None = None) -> dict:
    """A new actor and server on capture's state, with only the independently supplied question."""
    fresh = work / "fresh"
    stream = fresh / "stream.jsonl"
    row = {
        "question": case.recall_turn,
        **transport_metadata(credits),
        "client_mode": client_mode,
        "hook_invocations": None,
        "hook_starts": None,
        "argv": None,
        "settings": None,
        "stream": str(stream),
        "error": None,
        "session_id": None,
        "answer": None,
        "calls": [],
        "server_connected": None,
        "usage": None,
        "cost_usd": None,
    }
    try:
        for name in ("cwd", "hook-home"):
            (fresh / name).mkdir(parents=True)
        row["before"] = snapshot(work / "vault")
        files = stage_agent_files(arm, fresh, env, client_mode)
        row.update(argv=agent_argv(executable, files, model, credits), settings=json.loads(files["settings"].read_text(encoding="utf-8")))
        done = drive_session(
            row["argv"],
            fresh / "cwd",
            agent_env(fresh, case) | {"EXOMEM_CONFIG_PATH": env["EXOMEM_CONFIG_PATH"]},
            (case.recall_turn,),
            stream,
        )
        row.update(session_result(done, 1, client_mode))
        row["answer"] = row["replies"][-1] if row["replies"] else None
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        row["error"] = f"{type(error).__name__}: {error}"
    try:
        row["after"] = snapshot(work / "vault")
        if "before" in row:
            row["observed"] = diff_vault(row["before"], row["after"])
    except OSError as error:
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def run_session(
    case: Case,
    arm: Arm,
    *,
    repeat: int,
    model: str,
    out: Path,
    executable: Path,
    stamp: dict[str, str],
    hooked: bool = False,
    client_mode: str = DEFAULT_CLIENT_MODE,
    credits: CreditsRun | None = None,
) -> dict:
    if hooked and client_mode == "tool-only":
        raise ValueError("--hook-turn requires --client-mode plugin")
    work = out / "work" / client_mode / model / arm.label / f"{case.id}-{repeat}"
    shutil.rmtree(work, ignore_errors=True)
    for name in ("cwd", "hook-home", "logs", "scratch"):
        (work / name).mkdir(parents=True)
    env = pinned_env(arm, work, case)
    files = stage_agent_files(arm, work, env, client_mode)
    argv = agent_argv(executable, files, model, credits)
    row: dict = {
        "scenario": case.id,
        "repeat": repeat,
        "model": model,
        "arm": arm.label,
        "client_mode": client_mode,
        **transport_metadata(credits),
        "argv": argv,
        "settings": json.loads(files["settings"].read_text(encoding="utf-8")),
        "hook_invocations": None,
        "hook_starts": None,
        "product_sha": stamp["product_shas"][arm.label],
        "skill_sha256": hashlib.sha256(files["skill"].read_bytes()).hexdigest() if "skill" in files else None,
        "engagement": case.engagement,
        "turns": list(case.turns),
        "hooked": hooked,
        "stream": str(work / "stream.jsonl"),
        "recall_enabled": case.recall_turn is not None,
        "fresh": None,
        "error": None,
        **{k: v for k, v in stamp.items() if k != "product_shas"},
    }
    try:
        seed_vault(case, arm, work, env)
        before = snapshot(work / "vault")
        done = drive_session(
            argv,
            work / "cwd",
            agent_env(work, case),
            case.turns,
            work / "stream.jsonl",
            hook_turn=(lambda session_id: hook_ask(arm, session_id)) if hooked else None,
        )
        after = snapshot(work / "vault")
        row.update(session_result(done, len(case.turns) + hooked, client_mode))
        row.update(
            hook_turn=done.hook_turn,
            capture_before=before,
            capture_after=after,
            observed=diff_vault(before, after),
        )
        if case.recall_turn is not None:
            row["fresh"] = fresh_recall(case, arm, work, model=model, executable=executable, env=env, client_mode=client_mode, credits=credits)
            if row["fresh"].get("session_id") == row.get("session_id") and row.get("session_id"):
                row["fresh"]["error"] = "the fresh actor reused the capture session identity"
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def init_field(stdout: str, name: str) -> object:
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("subtype") == "init":
            return event.get(name)
    return None


def server_state(stdout: str) -> object:
    servers = init_field(stdout, "mcp_servers") or []
    return {s.get("name"): s.get("status") for s in servers if isinstance(s, dict)}


def run_all(
    cases: list[Case],
    *,
    model: str,
    repeats: int,
    out: Path,
    arms: list[Arm],
    repeat_offset: int = 0,
    hooked: bool = False,
    client_mode: str = DEFAULT_CLIENT_MODE,
    credits: CreditsRun | None = None,
) -> list[dict]:
    """Every case `repeats` times, the arms one after the other inside each repeat, so a slow hour falls on both."""
    if hooked and client_mode == "tool-only":
        raise ValueError("--hook-turn requires --client-mode plugin")
    out = refuse_unsafe_out_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    if credits:
        launcher = credits.executable or shutil.which("harness")
        if not launcher:
            raise SystemExit("the maintained credits launcher 'harness' was not found; no actor was started")
        executable, cli_version = Path(launcher), None
    else:
        envelope = discover_agent_envelope()
        executable, cli_version = envelope.executable, envelope.version
    stamp = {
        "cli_version": cli_version,
        "repo_sha": checkout_sha(REPO_ROOT),
        "product_shas": {arm.label: checkout_sha(arm.root) for arm in arms},
    }
    rows = []
    with (out / "runs.jsonl").open("a", encoding="utf-8") as sink:
        for case in cases:
            for repeat in range(repeat_offset, repeat_offset + repeats):
                for arm in arms:
                    row = run_session(
                        case,
                        arm,
                        repeat=repeat,
                        model=model,
                        out=out,
                        executable=executable,
                        stamp=stamp,
                        hooked=hooked,
                        client_mode=client_mode,
                        credits=credits,
                    )
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
                    rows.append(row)
                    observed = row.get("observed") or {}
                    print(
                        f"{client_mode} {model} {arm.label} {case.id}#{repeat}: "
                        f"{'error: ' + row['error'] if row['error'] else 'ran'}; "
                        f"changed={sorted(observed.get('changed', {}))} recaps={sorted(observed.get('recaps', {}))}",
                        file=sys.stderr,
                        flush=True,
                    )
    return rows


def dry_run(case: Case, arm: Arm, scratch: Path, hooked: bool = False, client_mode: str = DEFAULT_CLIENT_MODE) -> list[str]:
    """No model. The case file loads, and on this arm the seed lays down, the snapshot finds the seeded pages, and the
    server, skill and hook files stage; with `hooked`, the arm's episode ask reads from its hook script. Returns the
    problems found."""
    if hooked and client_mode == "tool-only":
        raise ValueError("--hook-turn requires --client-mode plugin")
    problems: list[str] = []
    work = scratch / client_mode / f"{arm.label}-{case.id}"
    shutil.rmtree(work, ignore_errors=True)
    for name in ("hook-home", "logs", "scratch"):
        (work / name).mkdir(parents=True)
    env = pinned_env(arm, work, case)
    try:
        seed_vault(case, arm, work, env)
        stage_agent_files(arm, work, env, client_mode)
        if hooked:
            hook_ask(arm, "dry-run")
    except (OSError, RuntimeError, KeyError, AttributeError, subprocess.TimeoutExpired) as error:
        return [f"{type(error).__name__}: {error}"]
    seeded = snapshot(work / "vault")
    wanted = {p.relative_to(case.seed).as_posix() for p in case.seed.rglob("*.md")}
    if wanted - set(seeded):
        problems.append(f"the seed pages {sorted(wanted - set(seeded))} are not in the vault")
    return problems
