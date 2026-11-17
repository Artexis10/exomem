"""Replay product incidents on real models, one fresh session per run, and compare code versions.

Each file under ``cases/`` holds one incident's cases. A case seeds one note through the arm's own
writer, sends one user turn to Claude Code in print mode (or to the OpenAI actor), and checks the outcome:

- ``seed_corrected``: the seeded note's body changed, or the arm's own status registry reports it
  superseded (a correction through either route counts);
- ``called_any``: the session called at least one of the named Exomem tools;
- ``judge``: a blind model fills the case's JSON schema from the user turn and the answer, and every
  ``require`` field must match. ``include_sources`` adds the Sources the session saved (title, kind, url,
  document form and identity check state); ``include_rows`` adds the Records rows the session committed
  (with ``collection_row``); ``reference`` states what the case's files really are.
- ``source_saved``, ``source_count``, ``current_source``, ``compiled_with_backlinks``: the vault's Sources
  and the pages citing them, read by the arm's own code (``arm_side.py sources``), matched to the case's
  files by the SHA-256 of the stored original. ``current_source`` compares stored frontmatter fields
  exactly (``contains``, ``not_contains``, ``eq``, ``ne``; case-insensitive) on the Sources that no
  correction receipt supersedes; ``if_none`` is what it settles to when no such Source exists (false
  by default, because a case that expects a Source has failed when none is kept).
- ``collection_row``: the rows of the Records collections that the case seeded, read by the arm's own records
  query (``arm_side.py records``) and compared with the rows the seed left. ``collection`` gains exactly
  ``equals`` new rows (one by default), each holding every ``where`` condition (the ``current_source``
  comparisons, over the row's fields), and no ``absent_in`` collection gains a row;
- ``write_receipt``: the tool results the session saw. Every receipt of a ``tools`` call that came before the
  session's first ``before`` call contains ``contains`` (true when the session made no such call), so a note
  written before the row has to carry the product's pointer to the Record that is still owed.

A case may declare ``files`` (built into the work directory at unit time, see ``fixtures.py``),
``seed_sources`` (captured as Sources before the session), ``seed_collections`` (Records collections created
before the session through the arm's own records command: ``manifest_path`` relative to the knowledge base, the
``manifest`` as a frontmatter mapping or whole text, optional ``body`` and ``rows``, and an optional
``method_page`` ``{title, note_type, content, revisions}`` written first so the manifest's ``capture.method``
link resolves, each revision replacing its body, so the last one is the page's current version), a capture
``prominence`` (set through the arm's own preference path, so the declared-route ``file`` disposition is
reachable; the shipped default applies when absent) and ``"transport": "http"``, which serves the arm over
loopback HTTP so the agent can send a file's bytes (``server.py``). Stdio is the default.

An arm is a checkout: its ``src`` runs the MCP server, its ``plugins/claude-code`` supplies the skill
and hooks, and its own docs and code supply the guidance text, in native MCP activation mode. Every
session gets its own vault, state and hook home under a neutral unit id; the arms run interleaved in
one window, and the arm of a unit is recorded only in ``results.jsonl``.

``--actor openai`` replaces Claude Code with an OpenAI Agents SDK agent on the same stdio server and no
hooks: the client class of a chat client that has MCP tools and the skill text but no hook surface (see
``openai_actor.py``). Its unit records token usage and the spend receipt of ``harness openai-credits``.

A unit passes (true), fails (false) or is unknown (null): a harness fault, a seeding error or a
judge error is unknown, never a failure.

    PYTHONPATH=benchmarks:src python -m membench.outcome.incidents \\
        --arm base=../exomem-base --arm changed=. --repeats 3 --run-root /var/tmp/exomem-outcome-evals/runs/incidents-<id>

The same run with the OpenAI actor, under an ``OPENAI_API_KEY`` for the eval account (the Agents SDK
layers over the interpreter that has the product's dependencies):

    PYTHONPATH=benchmarks:src uv run --no-project --python <that interpreter> --with openai-agents \\
        python -m membench.outcome.incidents --actor openai --arm changed=. --case existing-workflow --repeats 1 \\
        --run-root /var/tmp/exomem-outcome-evals/runs/incidents-<id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from epistemic.journeys.f27_replay import (
    DEFAULT_MAX_TURNS,
    Arm,
    JourneySetupError,
    build_turn_argv,
    count_hook_activity,
    discover_agent_envelope,
    fault_reason,
    refuse_unsafe_out_dir,
)
from epistemic.journeys.f33_referent_replay import tool_calls

from exomem import prominence as product_prominence
from membench.trackc.witness_join import parse_stream_json_transcript

from .. import billing
from ..faults import init_faults
from ..isolation import (
    ACTOR_MODEL,
    SHIPPED_PROMINENCE,
    child_env,
    claude_env,
    fresh_vault,
    hook_env,
    lay_instruction_block,
    mcp_server,
    plugin_read_allow,
)
from ..judging import Judge, JudgeSpec, grade_item
from ..record import checkout_state
from . import openai_actor
from .fixtures import KINDS, build_files
from .server import lay_exomem_command, loopback_mcp

CASES_DIR = Path(__file__).parent / "cases"
ARM_SIDE = Path(__file__).with_name("arm_side.py")
ARM_SIDE_TIMEOUT_SECONDS = 300
SESSION_TIMEOUT_SECONDS = 900
JUDGE_TIMEOUT_SECONDS = 300
SOURCE_CHECKS = ("source_saved", "source_count", "current_source", "compiled_with_backlinks")
#: The checks this runner implements; a case's `pass` names a subset.
CHECKS = (
    "seed_corrected",
    "called_any",
    "judge",
    "collection_row",
    "write_receipt",
    *SOURCE_CHECKS,
)
TRANSPORTS = ("stdio", "http")
#: The actors a unit may be played by: the runner's own two session drivers.
ACTORS = ("claude", "openai")
#: The comparisons of a `current_source` or `collection_row` condition: the checks' own closed set, not a vocabulary.
CONDITIONS = {
    "contains": lambda text, wanted: wanted in text,
    "not_contains": lambda text, wanted: wanted not in text,
    "eq": lambda text, wanted: text == wanted,
    "ne": lambda text, wanted: text != wanted,
}
JUDGE_PROMPT = (
    "You grade one assistant answer. Read the user's turn and the answer, then fill the schema.\n\n"
    "User turn: {turn}\n\nAnswer:\n<<<\n{answer}\n>>>{extra}"
)


def named_files(rule: dict) -> list[str]:
    """Every file name that a case's source checks refer to."""
    return [
        *rule.get("source_saved", ()),
        *rule.get("compiled_with_backlinks", ()),
        *(rule[key]["file"] for key in ("source_count", "current_source") if key in rule),
    ]


def validate_case(case: dict) -> None:
    """Refuse a case that could pass or fail on nothing, before any session starts."""
    rule, files = case["pass"], case.get("files", {})
    # A misspelled rule would leave a case with no checks, and it would pass every run.
    unknown = set(rule) - set(CHECKS)
    if unknown or not rule:
        raise SystemExit(
            f"case {case['id']}: pass rules must be among {CHECKS}, got {sorted(unknown)}"
        )
    spec = rule.get("judge")
    # An empty `require` would pass every answer, and a field the schema does not require may be absent.
    if spec and (
        not spec["require"] or set(spec["require"]) - set(spec["schema"].get("required", ()))
    ):
        raise SystemExit(
            f"case {case['id']}: judge `require` must be non-empty and within the schema's `required`"
        )
    if case.get("transport", "stdio") not in TRANSPORTS:
        raise SystemExit(f"case {case['id']}: transport must be one of {TRANSPORTS}")
    if "seed" not in case and "seed_corrected" in rule:
        raise SystemExit(f"case {case['id']}: `seed_corrected` needs a `seed` note")
    if {file["kind"] for file in files.values()} - KINDS:
        raise SystemExit(f"case {case['id']}: file kinds must be among {sorted(KINDS)}")
    seeded = [source["file"] for source in case.get("seed_sources", ()) if "file" in source]
    # A misspelled file name matches no Source, so a check that settles on "no Source" would pass it.
    missing = sorted(set(named_files(rule) + seeded) - set(files))
    if missing:
        raise SystemExit(
            f"case {case['id']}: files {missing} are named but not declared in `files`"
        )
    for source in case.get("seed_sources", ()):
        if ("file" in source) == ("content" in source) or not {"title", "source_kind"} <= set(
            source
        ):
            raise SystemExit(
                f"case {case['id']}: a seed source has a title, a source_kind and one of file or content"
            )
    if "current_source" in rule:
        conditions = rule["current_source"].get("where", ())
        if not conditions or any(c["op"] not in CONDITIONS for c in conditions):
            raise SystemExit(
                f"case {case['id']}: current_source needs conditions with ops among {sorted(CONDITIONS)}"
            )
    if "source_count" in rule and type(rule["source_count"].get("equals")) is not int:
        raise SystemExit(f"case {case['id']}: source_count needs an integer `equals`")
    validate_collections(case)


def validate_collections(case: dict) -> None:
    """Refuse a case whose seeded collections, prominence or row and receipt checks could not read what they name."""
    rule, seeded = case["pass"], case.get("seed_collections", ())
    # An unknown level would reach the arm's own preference writer and fail every unit the same way.
    if "prominence" in case and case["prominence"] not in product_prominence.CANON:
        raise SystemExit(f"case {case['id']}: prominence must be one of {product_prominence.CANON}")
    for collection in seeded:
        page = collection.get("method_page")
        if not {"manifest_path", "manifest"} <= set(collection) or (
            page is not None and not {"title", "note_type", "content"} <= set(page)
        ):
            raise SystemExit(
                f"case {case['id']}: a seed collection has a manifest_path, a manifest and, when it names a "
                "method page, that page's title, note_type and content"
            )
    if "collection_row" in rule:
        spec = rule["collection_row"]
        named = [spec.get("collection"), *spec.get("absent_in", ())]
        # A misspelled path reads no collection, so the check could never see a row it expects or forbids.
        if set(named) - {collection["manifest_path"] for collection in seeded}:
            raise SystemExit(
                f"case {case['id']}: collection_row names {named}, and `seed_collections` creates each of them"
            )
        if any(c["op"] not in CONDITIONS for c in spec.get("where", ())):
            raise SystemExit(
                f"case {case['id']}: collection_row conditions need ops among {sorted(CONDITIONS)}"
            )
        # A negative count could never hold, and a count that is not an int would compare unequal to every row count.
        if "equals" in spec and (type(spec["equals"]) is not int or spec["equals"] < 0):
            raise SystemExit(
                f"case {case['id']}: collection_row `equals` is an integer, zero or more"
            )
    if rule.get("judge", {}).get("include_rows") and "collection_row" not in rule:
        raise SystemExit(
            f"case {case['id']}: the judge's `include_rows` shows what `collection_row` reads"
        )
    if "write_receipt" in rule:
        spec = rule["write_receipt"]
        # An empty clause is contained in every receipt, so the check would pass whatever the product said.
        if not spec.get("tools") or not spec.get("contains"):
            raise SystemExit(
                f"case {case['id']}: write_receipt needs the `tools` whose receipts it reads and a non-empty `contains`"
            )


def load_cases(selected: list[str] | None, cases_dir: Path | None = None) -> list[dict]:
    cases = []
    # Read the module's CASES_DIR at call time, so a test that points it elsewhere is honoured.
    for path in sorted((cases_dir or CASES_DIR).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        # A case lists the files it uses by name; the file's `library` holds each spec once, for every case that shares it.
        library = document.get("library", {})
        for case in document["cases"]:
            absent = [name for name in case.get("files", ()) if name not in library]
            if absent:
                raise SystemExit(
                    f"case {case['id']}: files {absent} are not in {path.name}'s `library`"
                )
            cases.append({**case, "files": {name: library[name] for name in case.get("files", ())}})
    for case in cases:
        validate_case(case)
    unknown_ids = set(selected or ()) - {case["id"] for case in cases}
    if unknown_ids:
        raise SystemExit(
            f"no case with id {sorted(unknown_ids)}; the cases are {[case['id'] for case in cases]}"
        )
    return [case for case in cases if not selected or case["id"] in selected]


def run_arm_side(arm: Path, root: Path, *args: str, stdin: str | None = None) -> dict:
    """Run ``arm_side.py`` as the arm's own product code under the session's pins; its one JSON line."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(ARM_SIDE), *args],
        input=stdin,
        env=child_env(root, arm),
        capture_output=True,
        text=True,
        timeout=ARM_SIDE_TIMEOUT_SECONDS,
    )
    if done.returncode != 0:
        raise RuntimeError(f"arm_side {args[0]} failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def seed_corrected(before: dict, after: dict) -> bool | None:
    """Whether the seeded note was corrected: its body changed, or the product reports it superseded.

    A removed note counts as changed. None when the body is unchanged and the product's status
    registry could not classify the note, because then supersession is unknown.
    """
    if after["body"] != before["body"] or after["lifecycle_class"] == "superseded":
        return True
    return None if after["lifecycle_class"] is None else False


def holding(facts: dict, digest: str) -> list[dict]:
    """The Source rows whose stored original has this SHA-256."""
    return [row for row in facts["sources"] if row["original_sha256"] == digest]


def source_saved(facts: dict, fixtures: dict[str, str], names: list[str]) -> bool:
    """Every named file is held by at least one Source."""
    return all(holding(facts, fixtures[name]) for name in names)


def source_count(facts: dict, fixtures: dict[str, str], spec: dict) -> bool:
    """The named file's bytes are held by exactly ``equals`` Sources."""
    return len(holding(facts, fixtures[spec["file"]])) == spec["equals"]


def condition_holds(frontmatter: dict, condition: dict) -> bool:
    """One exact comparison on a stored frontmatter field; a missing field reads as empty text."""
    value = frontmatter.get(condition["field"])
    text = "" if value is None else str(value).casefold()
    return CONDITIONS[condition["op"]](text, str(condition["value"]).casefold())


def current_source(facts: dict, fixtures: dict[str, str], spec: dict) -> bool | None:
    """Every condition holds on each Source of the file that no correction receipt supersedes.

    When none remains the check settles to ``if_none`` (false unless the case says otherwise). None when
    the arm could not say whether a Source of the file was corrected.
    """
    held = holding(facts, fixtures[spec["file"]])
    if any(row["corrected"] is None for row in held):
        return None
    current = [row for row in held if not row["corrected"]]
    if not current:
        return spec.get("if_none", False)
    return all(condition_holds(row["frontmatter"], c) for row in current for c in spec["where"])


def compiled_with_backlinks(facts: dict, fixtures: dict[str, str], names: list[str]) -> bool | None:
    """A live page cites a Source of every named file, and each of those Sources lists that page back.

    None when a page that links both ways has a status the arm's registry cannot classify.
    """
    groups = [holding(facts, fixtures[name]) for name in names]
    linked = [
        page
        for page in facts["citing"]
        if all(
            any(
                row["key"] in page["sources"] and page["key"] in row["ingested_into"]
                for row in group
            )
            for group in groups
        )
    ]
    if any(page["live"] for page in linked):
        return True
    return None if any(page["live"] is None for page in linked) else False


def gained_rows(listed: dict, before: dict[str, list[str]], name: str) -> list[dict]:
    """The rows of the collection ``name`` that the seed did not leave: the session's own."""
    return [row for row in listed[name]["rows"] if row["record_id"] not in before[name]]


def collection_row(listed: dict, before: dict[str, list[str]], spec: dict) -> bool | None:
    """The collection gained exactly ``equals`` rows (one by default), each holding every condition, and no
    ``absent_in`` collection gained one.

    A row is new when the seed did not leave its ``record_id``. None when the arm could not list a collection
    the check reads: an unreadable collection is unknown, never an empty one.
    """
    named = [spec["collection"], *spec.get("absent_in", ())]
    if any("error" in listed[name] for name in named):
        return None
    rows = gained_rows(listed, before, spec["collection"])
    return (
        len(rows) == spec.get("equals", 1)
        and all(condition_holds(row, c) for row in rows for c in spec.get("where", ()))
        and not any(gained_rows(listed, before, name) for name in spec.get("absent_in", ()))
    )


def result_text(content: object) -> str:
    """A tool result as text: the string itself, or the text of each block of a block list."""
    if isinstance(content, list):
        return "\n".join(
            block["text"] for block in content if isinstance(block, dict) and "text" in block
        )
    return content if isinstance(content, str) else json.dumps(content, default=str)


def refused(call: dict) -> bool:
    """Whether a call saved nothing: the transport marked it an error, or the server answered with its error
    envelope, ``{"error": {...}}``, which the stdio server sends as the result when it refuses a write."""
    if call["is_error"]:
        return True
    try:
        body = json.loads(call["result"] or "")
    except ValueError:
        return False
    return isinstance(body, dict) and isinstance(body.get("error"), dict)


def write_receipt(calls: list[dict], spec: dict) -> bool | None:
    """Every receipt of a ``tools`` call that came before the first ``before`` call contains ``contains``.

    True when the session made no such call. A refused call left nothing saved, so it owes no receipt.
    None when a call that was owed a receipt has no result to read.
    """
    first = next(
        (index for index, call in enumerate(calls) if call["name"] in spec.get("before", ())),
        len(calls),
    )
    owed = [call for call in calls[:first] if call["name"] in spec["tools"] and not refused(call)]
    if any(call["result"] is None for call in owed):
        return None
    return all(spec["contains"] in result_text(call["result"]) for call in owed)


SOURCE_CHECK_FUNCTIONS = {
    "source_saved": source_saved,
    "source_count": source_count,
    "current_source": current_source,
    "compiled_with_backlinks": compiled_with_backlinks,
}
UNKNOWN_REASONS = {
    "current_source": "the arm could not say whether a Source of the file was corrected",
    "compiled_with_backlinks": "the arm's status registry could not classify a page that cites the Sources",
}


def session_sources(facts: dict, before: list[str]) -> list[dict]:
    """The Sources saved in the session, as the judge sees them: how each is labelled, not where it lives."""
    saved = []
    for row in facts["sources"]:
        if row["path"] in before:
            continue
        frontmatter = row["frontmatter"]
        shown = {
            "title": frontmatter.get("title"),
            "source_kind": frontmatter.get("source_type"),
            "url": frontmatter.get("url"),
        }
        if frontmatter.get("document_form"):
            shown["document_form"] = frontmatter["document_form"]
        check = frontmatter.get("identity_check")
        if isinstance(check, dict) and check.get("state"):
            shown["identity_check_state"] = check["state"]
        saved.append(shown)
    return saved


def judge_context(spec: dict, saved: list[dict] | None, rows: dict | None = None) -> str:
    """What the judge reads besides the turn and the answer; the same view for every arm."""
    parts = []
    if spec.get("include_rows"):
        shown = json.dumps(rows, indent=1, default=str) if rows else "none"
        parts.append(
            f"Records the assistant committed in this session, by collection, as stored:\n{shown}"
        )
    if spec.get("include_sources"):
        shown = json.dumps(saved, indent=1) if saved else "none"
        parts.append(f"Sources the assistant saved in this session, as stored:\n{shown}")
    if spec.get("reference"):
        parts.append(
            f"What the files really are (the assistant was not given this):\n{spec['reference']}"
        )
    return "".join(f"\n\n{part}" for part in parts)


def settle(checks: dict[str, bool | None]) -> bool | None:
    """Every check true passes, any check false fails, and an unknown with no failure is unknown."""
    if False in checks.values():
        return False
    return None if None in checks.values() else True


def init_event(stdout: str) -> dict:
    """The session's ``init`` event, or an empty dict when the stream has none."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            return event
    return {}


def reads_sources(case: dict) -> bool:
    """Whether the case's checks need the vault's Sources, before and after the session."""
    rule = case["pass"]
    return any(name in rule for name in SOURCE_CHECKS) or bool(
        rule.get("judge", {}).get("include_sources")
    )


def collection_paths(rule: dict) -> list[str]:
    """The Records collections that a ``collection_row`` check reads, as the case names them."""
    spec = rule.get("collection_row")
    return [spec["collection"], *spec.get("absent_in", ())] if spec else []


def seed_unit(case: dict, arm: Path, root: Path) -> tuple[dict | None, dict, Path]:
    """Seed the arm's vault, whoever plays the session. Returns (seeded note, unit record, work directory).

    The record keeps what a check compares the session's result with: the Sources and the Records rows that
    exist before the session, which are the seed's and not the session's to be judged on.
    """
    work, vault = root / "work", fresh_vault(root, arm)
    work.mkdir(parents=True)
    http = case.get("transport", "stdio") == "http"
    record = {
        "transport": "http" if http else "stdio",
        "fixtures": build_files(case.get("files", {}), work),
    }
    if "prominence" in case:
        # The arm's own preference path: its server and its hooks read the same pinned config file.
        record["capture_prominence"] = run_arm_side(arm, root, "prominence", case["prominence"])
    seeded = run_arm_side(
        arm, root, "seed", str(vault), str(work), stdin=json.dumps(case.get("seed"))
    )
    if case.get("seed_sources"):
        run_arm_side(
            arm, root, "seed_sources", str(vault), str(work), stdin=json.dumps(case["seed_sources"])
        )
    if case.get("seed_collections"):
        record["collections"] = run_arm_side(
            arm, root, "seed_collections", str(vault), stdin=json.dumps(case["seed_collections"])
        )
    if reads_sources(case):
        before = run_arm_side(arm, root, "sources", str(vault))
        record["sources_before"] = [row["path"] for row in before["sources"]]
    if "collection_row" in case["pass"]:
        listed = run_arm_side(arm, root, "records", str(vault), *collection_paths(case["pass"]))
        # A collection that cannot be listed before the session has no rows to tell the session's from.
        if any("error" in rows for rows in listed.values()):
            raise RuntimeError(
                f"a seeded collection could not be listed: {json.dumps(listed)[:300]}"
            )
        record["rows_before"] = {
            name: [row["record_id"] for row in rows["rows"]] for name, rows in listed.items()
        }
    return seeded, record, work


def play_claude(
    case: dict,
    arm: Path,
    root: Path,
    work: Path,
    record: dict,
    *,
    executable: Path,
    model: str,
    max_turns: int,
) -> dict:
    """Run one Claude Code session on the seeded vault.

    Returns the session as every actor does: ``answer``, ``tools`` (the Exomem tools called), ``calls``
    (each with its input and result), ``fault`` (why the session proves nothing, or None), ``record`` (what
    the unit keeps about the session) and ``extra`` (what the unit result adds).
    """
    block = root / "custom-instructions.txt"
    plugin = arm / "plugins" / "claude-code"
    http = record["transport"] == "http"
    level = case.get("prominence", SHIPPED_PROMINENCE)
    record["instruction_block"] = lay_instruction_block(block, arm, level)
    tools = ["Read", "Skill"]
    allowed = ["mcp__exomem", "Skill", "Read(./**)", plugin_read_allow(plugin)]
    # The server's native MCP activation mode: the hooks route through the tools, not inject text.
    env = {**claude_env()[0], **hook_env(root), "EXOMEM_RETRIEVE_INJECT": "mcp"}
    if http:
        # What the documented handoff needs from a client: a digest, a descriptor file, and a send. The upload
        # reply names a multipart POST as the send, so `curl` is allowed beside `exomem attach`; the server is
        # loopback and its bearer dies with the run.
        tools += ["Bash", "Write"]
        # An `Edit` rule covers the Write tool, and the descriptor goes into the work directory.
        allowed += ["Bash(exomem attach:*)", "Bash(sha256sum:*)", "Bash(curl:*)", "Edit(./**)"]
        env["PATH"] = f"{lay_exomem_command(root, arm)}{os.pathsep}{env['PATH']}"
    actor = Arm(
        arm_id="incident",
        prominence=level,
        tools=",".join(tools),
        uses_plugin=True,
        allowed_tools=",".join(allowed),
        uses_custom_instructions=True,
        configure_ref="membench.outcome.incidents",
    )

    def mint(session: dict) -> str:
        return run_arm_side(arm, root, "mint", stdin=json.dumps(session))["token"]

    with loopback_mcp(root, arm, mint) if http else nullcontext(mcp_server(root, arm)) as server:
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"exomem": server}}))
        argv = build_turn_argv(
            executable=executable,
            arm=actor,
            prompt=case["prompt"],
            mcp_config=mcp,
            session_id=None,
            model=model,
            max_turns=max_turns,
            first=True,
            append_system_prompt_file=block,
            plugin_dir=plugin,
        )
        start = time.monotonic()
        done = subprocess.run(  # noqa: S603 - argv from the shared turn builder
            argv, cwd=work, env=env, capture_output=True, text=True, timeout=SESSION_TIMEOUT_SECONDS
        )
    record["seconds"] = round(time.monotonic() - start, 1)
    record["claude_md_sha256"] = hashlib.sha256((work / "CLAUDE.md").read_bytes()).hexdigest()
    (root / "stream.jsonl").write_text(done.stdout)
    transcript = parse_stream_json_transcript(done.stdout.splitlines())
    init = init_event(done.stdout)
    record |= {
        "notional_usd": transcript.total_cost_usd,
        "model_reported": init.get("model"),
        "api_key_source": init.get("apiKeySource"),
        "billing": billing.of_key_source(init.get("apiKeySource")),
    }
    fault = fault_reason(done, transcript) or "; ".join(init_faults(init, model=model, exomem=True))
    invocations, firings = count_hook_activity(done.stdout)
    prefix = "mcp__exomem__"  # the tool-name prefix that the "exomem" server name in the MCP config fixes
    return {
        "answer": transcript.result_text.strip(),
        "tools": [use.name.removeprefix(prefix) for use in transcript.memory_tool_uses()],
        "calls": [
            {
                "name": call["name"].removeprefix(prefix),
                "input": call["input"],
                "result": None if call["result"] is None else result_text(call["result"]),
                "is_error": call["is_error"],
            }
            for call in tool_calls(done.stdout)
            if call["name"].startswith(prefix)
        ],
        "fault": fault or None,
        "extra": {"hook_invocations": invocations, "capture_nudge_firings": firings},
    }


def judge_answer(
    case: dict,
    answer: str,
    root: Path,
    judge: Judge,
    saved: list[dict] | None = None,
    rows: dict | None = None,
) -> tuple[bool | None, str | None]:
    """The judge check and, when it is not true or false on a verdict, the reason.

    An answer that is empty from a session that ran is a product failure and is not judged. A judge
    that gives no usable verdict leaves the check unknown.
    """
    if not answer:
        return False, "the answer is empty"
    spec = case["pass"]["judge"]
    row = grade_item(
        judge,
        root.name,
        JUDGE_PROMPT.format(
            turn=case["prompt"], answer=answer, extra=judge_context(spec, saved, rows)
        ),
        spec["schema"],
        out_dir=root,
        pass_name="verdicts",
        timeout=JUDGE_TIMEOUT_SECONDS,
        problem=lambda v: (
            None if set(spec["require"]) <= set(v) else "the verdict lacks a required field"
        ),
    )
    (root / "verdict.json").write_text(json.dumps(row, indent=2))
    if row["verdict"] is None:
        return None, f"judge error: {row['errors'][-1][:300]}"
    return all(row["verdict"].get(k) == v for k, v in spec["require"].items()), None


def run_unit(
    case: dict, arm: Path, root: Path, *, judge: Judge, actor: str = "claude", **session
) -> dict:
    """One fresh session. `passed` is true, false or null; null always comes with a `reason`."""
    seeded, record, work = seed_unit(case, arm, root)
    record["actor"] = actor
    played = PLAYERS[actor](case, arm, root, work, record, **session)
    if played["fault"]:
        return {**record, "passed": None, "reason": f"harness fault: {played['fault']}"}
    vault = str(root / "vault")
    rule, checks, reasons, extra = case["pass"], {}, [], dict(played.get("extra", {}))
    if seeded:
        after = run_arm_side(arm, root, "probe", vault, seeded["path"])
        extra["seed_after"] = {
            "exists": after["exists"],
            "lifecycle_class": after["lifecycle_class"],
        }
    if "seed_corrected" in rule:
        corrected = seed_corrected(seeded, after)
        checks["seed_corrected"] = (
            None if corrected is None else corrected == rule["seed_corrected"]
        )
        if corrected is None:
            reasons.append("the arm's status registry could not classify the seeded note")
    if "called_any" in rule:
        checks["called_any"] = bool(set(played["tools"]) & set(rule["called_any"]))
    if "collection_row" in rule:
        listed = run_arm_side(arm, root, "records", vault, *collection_paths(rule))
        extra["rows_after"] = {
            name: [row["record_id"] for row in rows.get("rows", ())]
            for name, rows in listed.items()
        }
        checks["collection_row"] = collection_row(
            listed, record["rows_before"], rule["collection_row"]
        )
        if checks["collection_row"] is None:
            reasons.append("the arm could not list a collection that the check reads")
    if "write_receipt" in rule:
        checks["write_receipt"] = write_receipt(played["calls"], rule["write_receipt"])
        if checks["write_receipt"] is None:
            reasons.append("a write that was owed a receipt has no result in the session")
    facts = saved = None
    committed = (
        {
            name: gained_rows(listed, record["rows_before"], name)
            for name in listed
            if "rows" in listed[name]
        }
        if "collection_row" in rule
        else None
    )
    if "sources_before" in record:
        facts = run_arm_side(arm, root, "sources", vault)
        saved = session_sources(facts, record["sources_before"])
        extra["session_sources"] = saved
        for name in SOURCE_CHECKS:
            if name in rule:
                checks[name] = SOURCE_CHECK_FUNCTIONS[name](facts, record["fixtures"], rule[name])
                if checks[name] is None:
                    reasons.append(UNKNOWN_REASONS[name])
    if "judge" in rule:
        checks["judge"], reason = judge_answer(
            case, played["answer"], root, judge, saved, committed
        )
        reasons += [reason] if reason else []
    return (
        record
        | extra
        | {
            "passed": settle(checks),
            "reason": "; ".join(reasons) or None,
            "checks": checks,
            "tools": played["tools"],
            "calls": played["calls"],
        }
    )


def play_openai(
    case: dict, arm: Path, root: Path, work: Path, record: dict, *, model: str, max_turns: int
) -> dict:
    """Run one OpenAI Agents SDK session on the seeded vault (``openai_actor.py``), as `play_claude` returns it."""
    return openai_actor.play(
        case,
        arm,
        root,
        record,
        server_instructions=run_arm_side(arm, root, "instructions")["server"],
        model=model,
        max_turns=max_turns,
        timeout=SESSION_TIMEOUT_SECONDS,
    )


#: The session drivers by actor name; each returns the session as `play_claude` documents.
PLAYERS = {"claude": play_claude, "openai": play_openai}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m membench.outcome.incidents")
    parser.add_argument("--arm", action="append", required=True, help="NAME=CHECKOUT, repeatable")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", action="append", help="case id to run; default all")
    parser.add_argument("--actor", choices=ACTORS, default="claude", help="who plays the session")
    parser.add_argument(
        "--model",
        default=None,
        help=f"default: {ACTOR_MODEL} (claude), {openai_actor.OPENAI_MODEL} (openai)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help=f"default: {DEFAULT_MAX_TURNS} (claude), {openai_actor.MAX_TURNS} (openai)",
    )
    parser.add_argument("--run-root", type=Path, required=True)
    # A held-out set lives outside the repository, so no prompt, fixture or iterating session sees it.
    parser.add_argument("--cases-dir", type=Path, default=None, help="directory of case files")
    args = parser.parse_args(argv)
    model = args.model or (ACTOR_MODEL if args.actor == "claude" else openai_actor.OPENAI_MODEL)
    max_turns = args.max_turns or (
        DEFAULT_MAX_TURNS if args.actor == "claude" else openai_actor.MAX_TURNS
    )
    if args.actor == "openai":
        openai_actor.require_sdk(os.environ)
    arms = {name: Path(path).resolve() for name, path in (item.split("=", 1) for item in args.arm)}
    for name, path in arms.items():
        if (
            not (path / "src" / "exomem").is_dir()
            or not (path / "plugins" / "claude-code").is_dir()
        ):
            raise SystemExit(f"--arm {name}: {path} holds no src/exomem and plugins/claude-code")
    cases = load_cases(args.case, args.cases_dir)
    # Refuses a credits declaration without a key before the run directory or any session exists.
    billing.require_billing_env(os.environ)
    try:
        run_root = refuse_unsafe_out_dir(args.run_root)
    except JourneySetupError as error:
        raise SystemExit(str(error)) from error
    if run_root.exists() and any(run_root.iterdir()):
        raise SystemExit(f"{run_root} already holds a run; use a new --run-root")
    (run_root / "judge").mkdir(parents=True)
    envelope = discover_agent_envelope()
    judge = Judge(JudgeSpec.of("claude", executable=str(envelope.executable)), run_root / "judge")
    manifest = {
        "cli": envelope.version,
        "actor": {"name": args.actor, "model_requested": model, "max_turns": max_turns},
        # The billing source every session of this run must report (``billing.py``).
        "billing": billing.declared(),
        "judge": judge.spec.record(),
        "cases": [case["id"] for case in cases],
        "repeats": args.repeats,
        "arms": {
            name: {"checkout": str(path), **checkout_state(path)} for name, path in arms.items()
        },
    }
    (run_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    session = {"model": model, "max_turns": max_turns}
    if args.actor == "claude":
        session["executable"] = envelope.executable
    tally: dict[tuple[str, str], Counter] = {}
    for rep in range(1, args.repeats + 1):
        for case in cases:
            for name in list(arms) if rep % 2 else list(reversed(arms)):
                # A neutral id: no session or judge directory carries the arm's name.
                unit = uuid.uuid4().hex[:12]
                try:
                    record = run_unit(
                        case,
                        arms[name],
                        run_root / "units" / unit,
                        judge=judge,
                        actor=args.actor,
                        **session,
                    )
                except Exception as error:  # noqa: BLE001 - a runner or seeding error is unknown, never a product failure
                    record = {"passed": None, "reason": f"error: {error!r:.400}"}
                record = {"unit": unit, "case": case["id"], "arm": name, "rep": rep, **record}
                with (run_root / "results.jsonl").open("a") as handle:
                    handle.write(json.dumps(record) + "\n")
                tally.setdefault((case["id"], name), Counter())[record["passed"]] += 1
                print(json.dumps(record), flush=True)
    for (case_id, name), counts in sorted(tally.items()):
        print(
            f"{case_id}\t{name}\t{counts[True]} passed\t{counts[False]} failed\t{counts[None]} unknown"
        )


if __name__ == "__main__":
    main()
