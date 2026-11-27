"""Replay product incidents on real models, one fresh session per run, and compare code versions.

Each file under ``cases/`` holds one incident's cases. A case seeds one note, or a list of notes, through the
arm's own writer, sends one user turn to Claude Code in print mode (or to the OpenAI actor), and checks the outcome:

- ``seed_corrected``: the seeded note's body changed, or the arm's own status registry reports it
  superseded (a correction through either route counts);
- ``called_any``: the session called at least one of the named Exomem tools;
- ``judge``: a blind model fills the case's JSON schema from the user turn and the answer, and every
  ``require`` field must match. ``include_sources`` adds the Sources the session saved (title, kind, url,
  document form, identity check state and whether a correction superseded it); ``include_rows`` adds the Records rows the session committed
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
  comparisons, over the row's fields), and no ``absent_in`` collection gains a row. ``rows_each`` (condition lists)
  needs a row of its own for each list, so with ``equals`` it says that every expected row exists exactly once;
- ``write_receipt``: the tool results the session saw. Every receipt of a ``tools`` call that came before the
  session's first ``before`` call contains ``contains`` (true when the session made no such call), so a note
  written before the row has to carry the product's pointer to the Record that is still owed.
- ``file_kept``: a list; for each entry, the named case file's bytes are kept by a Source or an Evidence page (``arm_side.py sources``
  reads both), and every ``where`` condition holds on each such page; ``equals`` instead counts those pages, so
  ``0`` says the file was not kept anywhere.
- ``sources_added``: the Sources the session added on top of ``sources_before`` number exactly ``equals``,
  whatever their bytes — an edited copy of a case file counts too.
- ``new_collection``: the collections that the seed did not leave (read by the arm's own discovery, ``arm_side.py
  collections``) number exactly ``equals`` (one by default), and each holds every ``where`` condition (on its
  ``title``, ``semantic_profile``, ``natural_key``, ``fields`` and ``manifest_path``), declares a natural key, has
  at most ``max_fields`` fields, holds at least ``min_rows`` rows that hold every ``row_where`` condition, has
  one row for each ``rows_each`` condition list, and has no two rows with the same natural key. The unit
  records, and no check gates on, whether the create receipt or the inventory says the collection was created
  automatically, so an arm without the flag compares on the outcome.
- ``no_new_collection``: the seed left every collection that exists.
- ``no_write``: the session made no call to a tool that writes (the arm's own surface classes each tool, ``arm_side.py
  write_tools``), whether the arm accepted it or refused it.
- ``follow_up_judge``: the ``judge`` mechanism on the answer of the case's ``follow_up`` session.
- ``pending_reconciled``: after a write whose acknowledgement the case withheld (``withhold_first_write_ack``), the
  agent's next call on that collection was that same write (same arguments, same replay identity) or a read of the
  collection, so the uncertain write was reconciled and not repeated as a new one. It reads the unit's wire log
  (``withhold.py``), not the actor's account of its calls, and the unit records which of the two the agent did.

A case gives ``prompt`` (one user turn) or ``turns`` (several, played in order in one session, ``turns.py``). A
``follow_up`` is one more user turn that a new session plays on the same vault after the main session ends; its
answer is judged by ``follow_up_judge``, and ``new_collection`` reads the vault as the follow-up leaves it.

A case may declare ``files`` (built into the work directory at unit time, see ``fixtures.py``),
``seed_sources`` (captured as Sources before the session), ``seed_collections`` (Records collections created
before the session through the arm's own records command: ``manifest_path`` relative to the knowledge base, the
``manifest`` as a frontmatter mapping or whole text, optional ``body`` and ``rows``, and an optional
``method_page`` ``{title, note_type, content, revisions}`` written first so the manifest's ``capture.method``
link resolves, each revision replacing its body, so the last one is the page's current version), a capture
``prominence`` (set through the arm's own preference path, so the declared-route ``file`` disposition is
reachable; the shipped default applies when absent) and ``"transport": "http"``, which serves the arm over
loopback HTTP (``server.py``): a long-lived server that the Claude actor's first turn waits to be warm, and
the only transport over which an agent can send a file's bytes (a case with ``files`` also gets the handoff
tools). Stdio is the default. ``"remote_owner": true`` makes the loopback sign-in act as the vault's owner,
which the arm needs to serve a routed collection's declared method to it. ``"store": true`` (an http case) gives the unit
a vault whose collection store is enrolled and served, as the personal service has it (``store.py``): a lease
coordinator of the unit's own, and one more collection that the unit's seed never counts as the session's. ``"git": true`` makes the work
directory a repository with every fixture committed, so a case can ask the agent to work on a repository file
without that counting as a handed file. ``"withhold_first_write_ack": true`` (an http case, with ``pending_reconciled``)
puts a transport between the actor and the unit's server that commits the main session's first write that the arm
accepts and hands the actor the arm's own ``MUTATION_ACKNOWLEDGEMENT_PENDING`` for it (``withhold.py``): an
interrupted write, which the product cannot be made to produce on a healthy write without degrading the whole
session. Both actors begin only once
``recall_context`` answers without an ``index_warming`` abstention (``warmup.py``); the unit records
``warm_seconds``, and a server that does not warm in time makes the unit unknown. An arm whose manifest does
not parse ``capture`` is seeded without the declaration and the unit records ``capture_supported: false``.
Every seeded unit then has the arm build its indexes, the typed graph included (the unit records
``index_build_seconds``), as a long-lived vault has them built.

An arm is a checkout: its ``src`` runs the MCP server, its ``plugins/claude-code`` supplies the skill
and hooks, and its own docs and code supply the guidance text, in native MCP activation mode. Two arms may
be the same checkout: ``--arm-env NAME=KEY=VALUE`` sets one Exomem setting for arm NAME only, in the arm-side
helper, the server, the ``exomem`` command and the hooks, and the manifest records it. Every
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
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext
from functools import partial
from pathlib import Path

from epistemic.journeys.f27_replay import (
    Arm,
    JourneySetupError,
    build_turn_argv,
    count_hook_activity,
    discover_agent_envelope,
    fault_reason,
    refuse_unsafe_out_dir,
)
from epistemic.journeys.f33_referent_replay import tool_calls

from exomem import envelope as product_envelope
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
    server_pins,
)
from ..judging import Judge, JudgeSpec, grade_item
from ..record import checkout_state
from . import openai_actor, warmup
from .fixtures import KINDS, build_files
from .server import lay_exomem_command, loopback_mcp
from .store import StoreUnit, store_unit
from .turns import transcript, user_turns
from .withhold import Withholder, proxied

CASES_DIR = Path(__file__).parent / "cases"
ARM_SIDE = Path(__file__).with_name("arm_side.py")
ARM_SIDE_TIMEOUT_SECONDS = 300
SESSION_TIMEOUT_SECONDS = 900
#: A handed-file custody turn costs 5-6 tool calls and a 2026-11-17 unit hit the 12-turn cap.
INCIDENT_MAX_TURNS = 20
JUDGE_TIMEOUT_SECONDS = 300
SOURCE_CHECKS = ("source_saved", "source_count", "current_source", "compiled_with_backlinks", "file_kept")
#: The checks this runner implements; a case's `pass` names a subset.
CHECKS = (
    "seed_corrected",
    "called_any",
    "judge",
    "collection_row",
    "write_receipt",
    "sources_added",
    "new_collection",
    "no_new_collection",
    "no_write",
    "pending_reconciled",
    "follow_up_judge",
    *SOURCE_CHECKS,
)
#: What a `new_collection` condition on the collection itself can read: the collection's own manifest facts.
COLLECTION_FIELDS = ("title", "semantic_profile", "natural_key", "fields", "manifest_path")
#: A row condition on this field reads every declared field's value as one text: the agent names its own fields.
ANY_FIELD = "*"
NEW_COLLECTION_KEYS = frozenset(
    {"equals", "where", "max_fields", "min_rows", "row_where", "rows_each"}
)
TRANSPORTS = ("stdio", "http")
#: The actors a unit may be played by: the runner's own two session drivers.
#: ``claude-hookless`` is the Claude Code actor with every hook switched off (``disableAllHooks``): the client class of
#: a chat client that has the MCP tools, the skills and the pasted block but no hook surface, on a Claude model.
ACTORS = ("claude", "claude-hookless", "openai")
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
        *(kept["file"] for kept in rule.get("file_kept", ())),
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
    for name in ("judge", "follow_up_judge"):
        spec = rule.get(name)
        # An empty `require` would pass every answer, and a field the schema does not require may be absent.
        if spec and (
            not spec["require"] or set(spec["require"]) - set(spec["schema"].get("required", ()))
        ):
            raise SystemExit(
                f"case {case['id']}: {name} `require` must be non-empty and within the schema's `required`"
            )
    validate_turns(case)
    if case.get("transport", "stdio") not in TRANSPORTS:
        raise SystemExit(f"case {case['id']}: transport must be one of {TRANSPORTS}")
    # Only the loopback server has a sign-in whose principal could be the owner.
    if "remote_owner" in case and (
        type(case["remote_owner"]) is not bool or case.get("transport") != "http"
    ):
        raise SystemExit(f"case {case['id']}: `remote_owner` is a boolean of an http case")
    # Only the loopback server is a long-lived service whose store thread the unit's coordinator guards.
    if "store" in case and (type(case["store"]) is not bool or case.get("transport") != "http"):
        raise SystemExit(f"case {case['id']}: `store` is a boolean of an http case")
    if "no_write" in rule and rule["no_write"] is not True:
        raise SystemExit(f"case {case['id']}: `no_write` is true")
    # The withholding transport is a proxy in front of the loopback server; only the check that reads its log needs it.
    if "withhold_first_write_ack" in case and (
        case["withhold_first_write_ack"] is not True or case.get("transport") != "http"
    ):
        raise SystemExit(f"case {case['id']}: `withhold_first_write_ack` is true, of an http case")
    if ("withhold_first_write_ack" in case) != ("pending_reconciled" in rule) or (
        "pending_reconciled" in rule and rule["pending_reconciled"] is not True
    ):
        raise SystemExit(
            f"case {case['id']}: `withhold_first_write_ack` and the `pending_reconciled: true` check come together"
        )
    if "seed" not in case and "seed_corrected" in rule:
        raise SystemExit(f"case {case['id']}: `seed_corrected` needs a `seed` note")
    validate_seed(case)
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
    for kept in rule.get("file_kept", ()):
        if ("equals" in kept) == ("where" in kept) or (
            "equals" in kept and type(kept["equals"]) is not int
        ) or any(c["op"] not in CONDITIONS for c in kept.get("where", ())):
            raise SystemExit(
                f"case {case['id']}: each file_kept entry needs an integer `equals` or `where` conditions, not both"
            )
    if "source_count" in rule and type(rule["source_count"].get("equals")) is not int:
        raise SystemExit(f"case {case['id']}: source_count needs an integer `equals`")
    if "sources_added" in rule and type(rule["sources_added"].get("equals")) is not int:
        raise SystemExit(f"case {case['id']}: sources_added needs an integer `equals`")
    if "git" in case and type(case["git"]) is not bool:
        raise SystemExit(f"case {case['id']}: `git` must be true or false")
    validate_collections(case)


def validate_turns(case: dict) -> None:
    """Refuse a case with no user turn, two ways to give them, or a follow-up that nothing judges."""
    rule = case["pass"]
    turns = case.get("turns")
    # Exactly one of the two: a `prompt` beside `turns` would leave it unclear which the actors play.
    if ("prompt" in case) == ("turns" in case):
        raise SystemExit(
            f"case {case['id']}: give `prompt` (one turn) or `turns` (several), not both or neither"
        )
    if "turns" in case and (
        not isinstance(turns, list)
        or not turns
        or any(not isinstance(t, str) or not t for t in turns)
    ):
        raise SystemExit(f"case {case['id']}: `turns` is a non-empty list of non-empty user turns")
    if "prompt" in case and (not isinstance(case["prompt"], str) or not case["prompt"]):
        raise SystemExit(f"case {case['id']}: `prompt` is a non-empty user turn")
    # A follow-up session whose answer no check reads costs a session and proves nothing; a judge with no
    # follow-up would have no answer to read.
    if ("follow_up" in case) != ("follow_up_judge" in rule):
        raise SystemExit(
            f"case {case['id']}: `follow_up` and the `follow_up_judge` check come together"
        )
    if "follow_up" in case and (not isinstance(case["follow_up"], str) or not case["follow_up"]):
        raise SystemExit(f"case {case['id']}: `follow_up` is a non-empty user turn")


def validate_seed(case: dict) -> None:
    """Refuse a seed that would write nothing, or that the arm's writer could not take, before any session starts."""
    seed = case.get("seed")
    notes = seed if isinstance(seed, list) else [seed]
    if isinstance(seed, list):
        # `seed_corrected` compares one note before and after; with several seeded there is no one it could mean.
        if "seed_corrected" in case["pass"]:
            raise SystemExit(
                f"case {case['id']}: `seed_corrected` reads one note, not a list `seed`"
            )
        if not seed or any(
            not isinstance(note, dict) or not {"title", "note_type", "content"} <= set(note)
            for note in seed
        ):
            raise SystemExit(
                f"case {case['id']}: a list `seed` is non-empty and each item is an object with a title, "
                "a note_type and content"
            )
    # The writer takes the two as one reviewed decision. Its own refusal names an unsupported disposition, and
    # it keeps no list the runner could import, so the value is passed on as given.
    if any(
        isinstance(note, dict)
        and ("relation_disposition" in note) != ("relation_review_reason" in note)
        for note in notes
    ):
        raise SystemExit(
            f"case {case['id']}: a seed note carries `relation_disposition` and `relation_review_reason` together"
        )


def validate_collections(case: dict) -> None:
    """Refuse a case whose seeded collections, prominence or row and receipt checks could not read what they name."""
    rule, seeded = case["pass"], case.get("seed_collections", ())
    # An unknown level would reach the arm's own preference writer and fail every unit the same way.
    if "prominence" in case and case["prominence"] not in product_prominence.CANON:
        raise SystemExit(f"case {case['id']}: prominence must be one of {product_prominence.CANON}")
    # The envelope's own range: a value outside it would fail every unit in the arm's setter.
    if "capture_disposition" in case and case["capture_disposition"] not in product_envelope.RANGES["proactive_capture"]:
        raise SystemExit(f"case {case['id']}: capture_disposition must be one of {product_envelope.RANGES['proactive_capture']}")
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
        validate_conditions(case, "collection_row `seed_row_now`", spec.get("seed_row_now", []))
        each = spec.get("rows_each", [])
        if not isinstance(each, list) or any(
            not isinstance(item, list) or not item for item in each
        ):
            raise SystemExit(
                f"case {case['id']}: collection_row `rows_each` is a list of non-empty condition lists"
            )
        for item in each:
            validate_conditions(case, "collection_row `rows_each`", item)
            # `*` reads the declared fields of a collection the agent made; a seeded collection's fields are named.
            if any(c["field"] == ANY_FIELD for c in item):
                raise SystemExit(
                    f"case {case['id']}: collection_row `rows_each` conditions name a field, not `{ANY_FIELD}`"
                )
        # A negative count could never hold, and a count that is not an int would compare unequal to every row count.
        if "equals" in spec and (type(spec["equals"]) is not int or spec["equals"] < 0):
            raise SystemExit(
                f"case {case['id']}: collection_row `equals` is an integer, zero or more"
            )
    if rule.get("judge", {}).get("include_rows") and not {"collection_row", "new_collection"} & set(
        rule
    ):
        raise SystemExit(
            f"case {case['id']}: the judge's `include_rows` shows what `collection_row` or `new_collection` reads"
        )
    # The follow-up answer is judged against the case's key; the rows are the main session's own to show.
    if {"include_rows", "include_calls"} & set(rule.get("follow_up_judge", {})):
        raise SystemExit(
            f"case {case['id']}: `follow_up_judge` takes no `include_rows` or `include_calls`"
        )
    calls = rule.get("judge", {}).get("include_calls")
    if calls is not None and (
        not isinstance(calls, list) or not calls or any(not isinstance(c, str) for c in calls)
    ):
        raise SystemExit(
            f"case {case['id']}: the judge's `include_calls` is a non-empty list of tool names"
        )
    validate_new_collections(case)
    if "write_receipt" in rule:
        spec = rule["write_receipt"]
        # An empty clause is contained in every receipt, so the check would pass whatever the product said.
        if not spec.get("tools") or not spec.get("contains"):
            raise SystemExit(
                f"case {case['id']}: write_receipt needs the `tools` whose receipts it reads and a non-empty `contains`"
            )


def validate_conditions(case: dict, name: str, conditions: object) -> None:
    """Refuse conditions the comparison could not read: each is ``{field, op, value}`` with an op of ``CONDITIONS``."""
    if not isinstance(conditions, list) or any(
        not isinstance(c, dict)
        or not {"field", "op", "value"} <= set(c)
        or c["op"] not in CONDITIONS
        for c in conditions
    ):
        raise SystemExit(
            f"case {case['id']}: {name} conditions are {{field, op, value}} with ops among {sorted(CONDITIONS)}"
        )


def validate_new_collections(case: dict) -> None:
    """Refuse a ``new_collection`` or ``no_new_collection`` check that could pass on nothing or never be read."""
    rule = case["pass"]
    if "new_collection" in rule and "no_new_collection" in rule:
        raise SystemExit(
            f"case {case['id']}: `new_collection` and `no_new_collection` contradict each other"
        )
    if "no_new_collection" in rule and rule["no_new_collection"] is not True:
        raise SystemExit(f"case {case['id']}: `no_new_collection` is true")
    if "new_collection" not in rule:
        return
    spec = rule["new_collection"]
    unknown = set(spec) - NEW_COLLECTION_KEYS
    if unknown or "max_fields" not in spec:
        raise SystemExit(
            f"case {case['id']}: new_collection takes {sorted(NEW_COLLECTION_KEYS)} and needs `max_fields`; "
            f"got {sorted(unknown)}"
        )
    # A count that could never hold, or a bound of no fields, would fail every unit the same way.
    for key, floor in (("equals", 1), ("max_fields", 1), ("min_rows", 1)):
        if key in spec and (type(spec[key]) is not int or spec[key] < floor):
            raise SystemExit(
                f"case {case['id']}: new_collection `{key}` is an integer, {floor} or more"
            )
    validate_conditions(case, "new_collection `where`", spec.get("where", []))
    validate_conditions(case, "new_collection `row_where`", spec.get("row_where", []))
    each = spec.get("rows_each", [])
    if not isinstance(each, list) or any(not isinstance(item, list) or not item for item in each):
        raise SystemExit(
            f"case {case['id']}: new_collection `rows_each` is a list of non-empty condition lists"
        )
    for item in each:
        validate_conditions(case, "new_collection `rows_each`", item)
    # Row conditions read the row, and collection conditions read the collection's own fields.
    if any(c["field"] not in COLLECTION_FIELDS for c in spec.get("where", [])):
        raise SystemExit(
            f"case {case['id']}: new_collection `where` reads one of {sorted(COLLECTION_FIELDS)}"
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


def run_arm_side(
    arm: Path,
    root: Path,
    *args: str,
    stdin: str | None = None,
    arm_env: Mapping[str, str] | None = None,
) -> dict:
    """Run ``arm_side.py`` as the arm's own product code under the session's pins and the arm's ``arm_env``; its one JSON line."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(ARM_SIDE), *args],
        input=stdin,
        env=child_env(root, arm, arm_env),
        # The unit root holds no `.env`; the caller's directory may, and product code loads it over `env`.
        cwd=root,
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


def file_kept(facts: dict, fixtures: dict[str, str], specs: list[dict]) -> bool:
    """For each spec, the named file's bytes are kept by a Source or an Evidence page, and every ``where``
    condition holds on each such page. With ``equals``, exactly that many pages keep the bytes (0: not kept).
    """
    def holds(spec: dict) -> bool:
        digest = fixtures[spec["file"]]
        held = holding(facts, digest) + [
            row for row in facts.get("evidence", ()) if row["original_sha256"] == digest
        ]
        if "equals" in spec:
            return len(held) == spec["equals"]
        return bool(held) and all(
            condition_holds(row["frontmatter"], c) for row in held for c in spec["where"]
        )

    return all(holds(spec) for spec in specs)


def sources_added(facts: dict, before: list[str], spec: dict) -> bool:
    """The Sources the session added — rows not in ``sources_before`` — number exactly ``equals``.

    Every added Source counts whatever its bytes: an edited copy of a case file is still a Source.
    """
    return len([row for row in facts["sources"] if row["path"] not in before]) == spec["equals"]


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
    """The collection gained exactly ``equals`` rows (one by default), each holding every condition, a row of its
    own for each ``rows_each`` list, and no ``absent_in`` collection gained one.

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
        and one_row_each(rows, [], spec.get("rows_each", []))
        and not any(gained_rows(listed, before, name) for name in spec.get("absent_in", ()))
        and seed_row_now(listed, before, spec)
    )


def seed_row_now(listed: dict, before: dict[str, list[str]], spec: dict) -> bool:
    """A row that the seed left now holds every ``seed_row_now`` condition (true when the spec names none).

    A corrected value keeps the row's ``record_id``, so it is no new row; this reads the row that stayed.
    """
    conditions = spec.get("seed_row_now")
    if not conditions:
        return True
    name = spec["collection"]
    return any(
        row["record_id"] in before[name] and all(condition_holds(row, c) for c in conditions)
        for row in listed[name]["rows"]
    )


def gained_collections(facts: dict, before: list[str]) -> list[dict]:
    """The collections of the vault that the seed did not leave: the session's own."""
    return [collection for collection in facts["collections"] if collection["path"] not in before]


def row_condition_holds(row: dict, fields: list[str], condition: dict) -> bool:
    """One comparison on a row's field; ``*`` reads the declared fields' values as one text, because the agent
    chose their names."""
    if condition["field"] != ANY_FIELD:
        return condition_holds(row, condition)
    text = " ".join("" if row.get(field) is None else str(row[field]) for field in fields)
    return condition_holds({ANY_FIELD: text}, condition)


def natural_key_of(row: dict, key: list[str]) -> tuple:
    """A row's natural key as the product keys it: the declared fields' values, compared as trimmed text."""
    return tuple(
        "" if row.get(field) is None else str(row[field]).strip().casefold() for field in key
    )


def one_row_each(rows: list[dict], fields: list[str], lists: list[list[dict]]) -> bool:
    """Every condition list is held by a row of its own: no row serves two lists."""
    candidates = [
        [
            i
            for i, row in enumerate(rows)
            if all(row_condition_holds(row, fields, c) for c in conditions)
        ]
        for conditions in lists
    ]

    def place(n: int, used: frozenset) -> bool:
        return n == len(candidates) or any(
            place(n + 1, used | {i}) for i in candidates[n] if i not in used
        )

    return place(0, frozenset())


def collection_holds(collection: dict, spec: dict) -> bool | None:
    """One new collection meets the spec; None when the arm could not read its rows.

    It holds every ``where`` condition on its own facts, declares a natural key, has at most ``max_fields``
    fields, holds at least ``min_rows`` rows that hold every ``row_where`` condition, has a row of its own for
    each ``rows_each`` list, and has no two rows with the same natural key.
    """
    facts = {
        "title": collection["title"],
        "semantic_profile": collection["semantic_profile"],
        "natural_key": ", ".join(collection["natural_key"]),
        "fields": ", ".join(collection["fields"]),
        "manifest_path": collection["path"],
    }
    structure = (
        all(condition_holds(facts, c) for c in spec.get("where", ()))
        and bool(collection["natural_key"])
        and len(collection["fields"]) <= spec["max_fields"]
    )
    rows = collection["rows"]
    # A collection that is not Records has no rows to read: it is not the one a case expects.
    if rows is None:
        return False
    if isinstance(rows, dict):
        return None if structure else False
    fields, key = collection["fields"], collection["natural_key"]
    holding_rows = [
        row
        for row in rows
        if all(row_condition_holds(row, fields, c) for c in spec.get("row_where", ()))
    ]
    keys = [natural_key_of(row, key) for row in rows]
    return (
        structure
        and len(holding_rows) >= spec.get("min_rows", 1)
        and one_row_each(rows, fields, spec.get("rows_each", []))
        and len(set(keys)) == len(keys)
    )


def new_collection(facts: dict, before: list[str], spec: dict) -> bool | None:
    """Exactly ``equals`` collections (one by default) exist that the seed did not leave, and each meets the spec.

    None when the arm could not list the vault's collections, or could not read the rows of one that it
    lists: an unreadable vault is unknown, never an empty one.
    """
    if "error" in facts:
        return None
    gained = gained_collections(facts, before)
    if len(gained) != spec.get("equals", 1):
        return False
    verdicts = [collection_holds(collection, spec) for collection in gained]
    return False if False in verdicts else None if None in verdicts else True


def no_new_collection(facts: dict, before: list[str]) -> bool | None:
    """No collection exists that the seed did not leave; None when the arm could not list them."""
    if "error" in facts:
        return None
    return not gained_collections(facts, before)


def no_write(calls: list[dict], writers: list[str]) -> bool:
    """The session called none of the tools in ``writers``: the arm's own list of the tools that write.

    A call that the arm refused counts too: the session tried to write, and ``prominence: off`` asks it not to.
    """
    return not any(call["name"] in writers for call in calls)


def pending_outcome(wire: list[dict], writers: list[str], natural_key: list[str] | None = None) -> str:
    """What the agent did next on the row of the write whose acknowledgement was withheld.

    ``wire`` is the unit's wire log (``withhold.py``): every tool call of the main session in order, with its
    arguments and replay identity, and the one withheld write marked. The calls that follow it and name the same
    ``collection`` are the agent's calls on that collection. A write whose ``item`` differs from the withheld one on
    the collection's ``natural_key`` is another row, independent work the served recovery recipe allows, so it is
    skipped; a write that names no item, or a collection whose key is unknown, counts as touching the row. The first
    call that remains decides:

    - ``replayed``: the same write, with the same arguments and the same replay identity;
    - ``read``: a call to a tool that does not write (the arm's own list), such as a query of the collection;
    - ``other_write``: a write of the same row under a new identity or with a different payload;
    - ``none``: the agent made no later call on the collection;
    - ``no_collection``: the withheld write names no collection, so the case has nothing to recover on;
    - ``no_pending``: no write was withheld, because the agent made no write that the arm accepted.
    """
    held = next((i for i, call in enumerate(wire) if call["withheld"]), None)
    if held is None:
        return "no_pending"
    write = wire[held]
    collection = write["arguments"].get("collection")
    if collection is None:
        return "no_collection"
    def row_key(arguments: dict) -> tuple | None:
        item = arguments.get("item")
        return tuple(item.get(field) for field in natural_key) if natural_key and isinstance(item, dict) else None

    for call in wire[held + 1 :]:
        if call["arguments"].get("collection") != collection:
            continue
        if all(call[key] == write[key] for key in ("tool", "arguments", "identity")):
            return "replayed"
        if call["tool"] not in writers:
            return "read"
        key = row_key(call["arguments"])
        if key is not None and key != row_key(write["arguments"]):
            continue
        return "other_write"
    return "none"


def pending_reconciled(outcome: str) -> bool | None:
    """The agent replayed the uncertain write or read the collection; unknown when the case had nothing to recover on."""
    return None if outcome == "no_collection" else outcome in ("replayed", "read")


def automatic_flag(receipt: object) -> bool | None:
    """The ``automatic`` flag a receipt carries, wherever in it the product puts one; None when it has none."""
    if isinstance(receipt, dict):
        if isinstance(receipt.get("automatic"), bool):
            return receipt["automatic"]
        children = list(receipt.values())
    elif isinstance(receipt, list):
        children = receipt
    else:
        return None
    return next((flag for flag in map(automatic_flag, children) if flag is not None), None)


def receipt_says_automatic(calls: list[dict], collection: dict) -> bool | None:
    """Whether the session's receipt for creating ``collection`` says it was created automatically.

    The receipt is the result of the call that names the collection by its id or by the manifest path that the
    call sent. None when no such receipt carries the flag: an arm without it, or a creation that was refused.
    """
    for call in calls:
        if call["result"] is None or refused(call):
            continue
        named = collection["collection_id"] in call["result"] or (
            collection["kb_path"] is not None
            and collection["kb_path"] in json.dumps(call["input"], default=str)
        )
        if not named:
            continue
        try:
            flag = automatic_flag(json.loads(call["result"]))
        except ValueError:
            continue
        if flag is not None:
            return flag
    return None


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
    "file_kept": file_kept,
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
        # A person sees a corrected Source as superseded (search names its successor), so the judge does too.
        if row.get("corrected"):
            shown["superseded_by_a_correction"] = True
        saved.append(shown)
    return saved


def judge_context(
    spec: dict, saved: list[dict] | None, rows: dict | None = None, calls: list[dict] | None = None
) -> str:
    """What the judge reads besides the turn and the answer; the same view for every arm."""
    parts = []
    if spec.get("include_calls"):
        shown = [
            {"tool": call["name"], "input": call["input"], "refused": refused(call)}
            for call in calls or ()
            if call["name"] in spec["include_calls"]
        ]
        parts.append(
            "Writes the assistant attempted in this session, in order, as it sent them "
            f"(a refused call saved nothing):\n{json.dumps(shown, indent=1, default=str) if shown else 'none'}"
        )
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
    if spec.get("key"):
        parts.append(f"What is true (the assistant was not given this):\n{spec['key']}")
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
    return (
        "sources_added" in rule
        or any(name in rule for name in SOURCE_CHECKS)
        or bool(rule.get("judge", {}).get("include_sources"))
    )


def init_repo(work: Path) -> None:
    """Make the unit's work directory a git repository with every fixture committed.

    ``GIT_CONFIG_GLOBAL=/dev/null`` and ``GIT_CONFIG_NOSYSTEM=1`` keep a developer's own config and
    hooks out; the commit's identity comes from ``-c`` flags, not config.
    """
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(work),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    identity = ["-c", "user.name=bench", "-c", "user.email=bench@invalid"]
    for argv in (
        ["git", "init", "-q"],
        ["git", *identity, "add", "-A"],
        ["git", *identity, "commit", "-q", "-m", "fixtures"],
    ):
        subprocess.run(argv, cwd=work, env=env, check=True, capture_output=True)  # noqa: S603 - fixed argv


def reads_collections(case: dict) -> bool:
    """Whether the case's checks need the vault's collections as they stand before and after the session."""
    return bool({"new_collection", "no_new_collection"} & set(case["pass"]))


def collection_paths(rule: dict) -> list[str]:
    """The Records collections that a ``collection_row`` check reads, as the case names them."""
    spec = rule.get("collection_row")
    return [spec["collection"], *spec.get("absent_in", ())] if spec else []


def seed_unit(
    case: dict,
    arm: Path,
    root: Path,
    arm_env: Mapping[str, str] | None = None,
    store: StoreUnit | None = None,
) -> tuple[dict | list[dict] | None, dict, Path]:
    """Seed the arm's vault, whoever plays the session. Returns (seeded note or notes, unit record, work directory).

    The record keeps what a check compares the session's result with: the Sources and the Records rows that
    exist before the session, which are the seed's and not the session's to be judged on. With a ``store`` the
    seed is written as files first, as the product's own tests do, and the vault's store is then enrolled
    (``store.py``): from that point the arm's commands run under the unit's lease coordinator.
    """
    work, vault = root / "work", fresh_vault(root, arm, arm_env)
    arm_side = partial(run_arm_side, arm, root, arm_env=arm_env)
    work.mkdir(parents=True)
    http = case.get("transport", "stdio") == "http"
    record = {
        "transport": "http" if http else "stdio",
        "fixtures": build_files(case.get("files", {}), work),
    }
    if case.get("git"):
        init_repo(work)
        record["git"] = True
    if "prominence" in case:
        # The arm's own preference path: its server and its hooks read the same pinned config file.
        record["capture_prominence"] = arm_side("prominence", case["prominence"])
    if "capture_disposition" in case:
        # After the prominence, so the owner's explicit capture setting is the one in force.
        record["capture_disposition"] = arm_side("capture_disposition", case["capture_disposition"])
    seeded = arm_side("seed", str(vault), str(work), stdin=json.dumps(case.get("seed")))
    if case.get("seed_sources"):
        arm_side("seed_sources", str(vault), str(work), stdin=json.dumps(case["seed_sources"]))
    if case.get("seed_collections"):
        record["collections"] = arm_side(
            "seed_collections", str(vault), stdin=json.dumps(case["seed_collections"])
        )
        # One property of the arm, read once by its own code: whether its manifest carries `capture`.
        record["capture_supported"] = record["collections"][0]["capture_supported"]
    # A real vault has its indexes built, the typed graph included; without them recall serves no
    # connections. The arm's own classes build them, the same way for every arm and every case.
    record["index_build_seconds"] = arm_side("build_indexes", str(vault))["seconds"]
    if store:
        record["store"] = store.enrol(vault, arm_env)
        arm_side = partial(run_arm_side, arm, root, arm_env=store.environment(arm_env))
    if reads_sources(case):
        before = arm_side("sources", str(vault))
        record["sources_before"] = [row["path"] for row in before["sources"]]
    if reads_collections(case):
        before = arm_side("collections", str(vault))
        # A vault that cannot be listed has no collections to tell the session's from.
        if "error" in before:
            raise RuntimeError(f"the seeded collections could not be listed: {before['error']}")
        record["collections_before"] = [collection["path"] for collection in before["collections"]]
    if "collection_row" in case["pass"]:
        listed = arm_side("records", str(vault), *collection_paths(case["pass"]))
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
    arm_env: Mapping[str, str] | None = None,
    withhold: dict | None = None,
    hooks: bool = True,
) -> dict:
    """Run one Claude Code session on the seeded vault.

    With ``hooks`` false every hook is switched off through the session's settings (``disableAllHooks``), so
    the plugin supplies its skills only and the unit records ``hooks: false``.

    With ``withhold`` (``arm_side.py withhold``) the loopback server is served through the proxy that withholds the
    main session's first committed write's acknowledgement (``withhold.py``); the follow-up is not interrupted.

    Returns the session as every actor does: ``answer``, ``tools`` (the Exomem tools called), ``calls``
    (each with its input and result), ``fault`` (why the session proves nothing, or None), ``record`` (what
    the unit keeps about the session) and ``extra`` (what the unit result adds).
    """
    block = root / "custom-instructions.txt"
    plugin = arm / "plugins" / "claude-code"
    http = record["transport"] == "http"
    level = case.get("prominence", SHIPPED_PROMINENCE)
    record["instruction_block"] = lay_instruction_block(block, arm, level)
    record["hooks"] = hooks
    settings = None
    if not hooks:
        settings = root / "settings.json"
        settings.write_text(json.dumps({"disableAllHooks": True}))
    tools = ["Read", "Skill"]
    allowed = ["mcp__exomem", "Skill", "Read(./**)", plugin_read_allow(plugin)]
    # The server's native MCP activation mode: the hooks route through the tools, not inject text.
    env = {
        **claude_env()[0],
        **hook_env(root),
        "EXOMEM_RETRIEVE_INJECT": "mcp",
        # The hooks inherit this environment and run the arm's code, so its switch settings reach them here.
        **(arm_env or {}),
    }
    if case.get("files"):
        # What the documented handoff needs from a client: a digest, a descriptor file, and a send. The upload
        # reply names a multipart POST as the send, so `curl` is allowed beside `exomem attach`; the server is
        # loopback and its bearer dies with the run. A case with no file to hand over gets none of it.
        tools += ["Bash", "Write"]
        # An `Edit` rule covers the Write tool, and the descriptor goes into the work directory.
        allowed += ["Bash(exomem attach:*)", "Bash(sha256sum:*)", "Bash(curl:*)", "Edit(./**)"]
        env["PATH"] = f"{lay_exomem_command(root, arm, arm_env)}{os.pathsep}{env['PATH']}"
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
        return run_arm_side(arm, root, "mint", stdin=json.dumps(session), arm_env=arm_env)["token"]

    serving = (
        loopback_mcp(root, arm, mint, remote_owner=case.get("remote_owner", False), arm_env=arm_env)
        if http
        else nullcontext(mcp_server(root, arm, arm_env))
    )
    turns, follow_up = user_turns(case), case.get("follow_up")
    # One turn runs unpersisted, as it always has. Several are one session that each turn resumes by its id.
    main_session = str(uuid.uuid4()) if len(turns) > 1 else None
    sessions = [(turns, main_session, env)]
    if follow_up is not None:
        # A new session on the same vault, from the hook's initial state: nothing of the main session's
        # cooldowns reaches it, as on a later day.
        sessions.append(([follow_up], None, {**env, **hook_env(root, session="follow-up")}))
    results: list[list[dict]] = []
    spent: list[float | None] = []
    withholder = Withholder(withhold, root / "wire.json") if withhold else None
    with (
        serving as server,
        proxied(server, withholder) if withholder else nullcontext(server) as server,
    ):
        if http:
            # The loopback server is the long-lived service that the client talks to, so the turn starts on a
            # warm one, and the follow-up session meets the same server. A stdio server is started by Claude
            # Code itself and cannot be warmed beforehand.
            try:
                asyncio.run(warmup.await_warm(warmup.ask_over(server), record))
            except warmup.StillWarming as error:
                return {"answer": "", "tools": [], "calls": [], "fault": str(error)}
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"exomem": server}}))
        started = time.monotonic()
        for number, (session_turns, session_id, session_env) in enumerate(sessions):
            if number and withholder:
                withholder.disarm()
            results.append([])
            for index, prompt in enumerate(session_turns):
                argv = build_turn_argv(
                    executable=executable,
                    arm=actor,
                    prompt=prompt,
                    mcp_config=mcp,
                    session_id=session_id,
                    model=model,
                    max_turns=max_turns,
                    first=index == 0,
                    append_system_prompt_file=block,
                    plugin_dir=plugin,
                    settings_file=settings,
                )
                done = subprocess.run(  # noqa: S603 - argv from the shared turn builder
                    argv,
                    cwd=work,
                    env=session_env,
                    capture_output=True,
                    text=True,
                    timeout=SESSION_TIMEOUT_SECONDS,
                )
                # One turn and no follow-up keeps its stream where it always was; a longer run keeps one per turn.
                label = (
                    "stream.jsonl"
                    if len(turns) == 1 and follow_up is None
                    else f"stream-{'follow-up' if number else index + 1}.jsonl"
                )
                (root / label).write_text(done.stdout)
                turn = read_claude_turn(done, model)
                results[number].append(turn)
                spent.append(turn["notional_usd"])
                record["seconds"] = round(time.monotonic() - started, 1)
                if turn["fault"]:
                    break
            if any(turn["fault"] for turn in results[number]):
                break
    record["claude_md_sha256"] = hashlib.sha256((work / "CLAUDE.md").read_bytes()).hexdigest()
    first = results[0][0]
    init = first["init"]
    record |= {
        # None when any turn's cost is unknown: a partial sum would read as the whole.
        "notional_usd": None if None in spent else sum(spent),
        "model_reported": init.get("model"),
        "api_key_source": init.get("apiKeySource"),
        "billing": billing.of_key_source(init.get("apiKeySource")),
    }
    every = [turn for session in results for turn in session]
    main = results[0]
    return {
        "answer": main[-1]["answer"],
        "answers": [turn["answer"] for turn in main],
        "tools": [name for turn in main for name in turn["tools"]],
        "calls": [call for turn in main for call in turn["calls"]],
        "follow_up": (
            {
                "answer": results[1][0]["answer"],
                "tools": results[1][0]["tools"],
                "calls": results[1][0]["calls"],
            }
            if len(results) > 1 and results[1]
            else None
        ),
        # A hookless session in which a hook ran measured the hooked client, so it proves nothing.
        "fault": next((turn["fault"] for turn in every if turn["fault"]), None)
        or (None if hooks or not any(turn["hook_invocations"] for turn in every) else "a hook ran in a hookless session"),
        "extra": {
            "hook_invocations": sum(turn["hook_invocations"] for turn in every),
            "capture_nudge_firings": sum(turn["capture_nudge_firings"] for turn in every),
        },
    }


def read_claude_turn(done: subprocess.CompletedProcess, model: str) -> dict:
    """One Claude Code turn as the actor reports it: its answer, the Exomem tool calls, its cost and any harness fault.

    Every turn's ``init`` event is checked, because it arrives on every user turn.
    """
    transcript = parse_stream_json_transcript(done.stdout.splitlines())
    init = init_event(done.stdout)
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
        "init": init,
        "notional_usd": transcript.total_cost_usd,
        "hook_invocations": invocations,
        "capture_nudge_firings": firings,
    }


def judge_answer(
    case: dict,
    answer: str,
    root: Path,
    judge: Judge,
    saved: list[dict] | None = None,
    rows: dict | None = None,
    *,
    calls: list[dict] | None = None,
    turn: str | None = None,
    name: str = "judge",
) -> tuple[bool | None, str | None]:
    """The judge check and, when it is not true or false on a verdict, the reason.

    An answer that is empty from a session that ran is a product failure and is not judged. A judge
    that gives no usable verdict leaves the check unknown. ``turn`` is what the judge reads as the user's
    turn (the case's ``prompt`` by default); ``name`` is the check, ``judge`` or ``follow_up_judge``, which
    keeps the second verdict of a unit apart from the first.
    """
    if not answer:
        return False, "the answer is empty"
    spec = case["pass"][name]
    # The first verdict keeps its file and item id; the follow-up's get a suffix, so the two never overwrite.
    suffix = "" if name == "judge" else f".{name.removesuffix('_judge').replace('_', '-')}"
    row = grade_item(
        judge,
        root.name + suffix,
        JUDGE_PROMPT.format(
            turn=case["prompt"] if turn is None else turn,
            answer=answer,
            extra=judge_context(spec, saved, rows, calls),
        ),
        spec["schema"],
        out_dir=root,
        pass_name="verdicts",
        timeout=JUDGE_TIMEOUT_SECONDS,
        problem=lambda v: (
            None if set(spec["require"]) <= set(v) else "the verdict lacks a required field"
        ),
    )
    (root / f"verdict{suffix}.json").write_text(json.dumps(row, indent=2))
    if row["verdict"] is None:
        return None, f"judge error: {row['errors'][-1][:300]}"
    return all(row["verdict"].get(k) == v for k, v in spec["require"].items()), None


def run_unit(
    case: dict,
    arm: Path,
    root: Path,
    *,
    judge: Judge,
    actor: str = "claude",
    arm_env: Mapping[str, str] | None = None,
    **session,
) -> dict:
    """One fresh session. `passed` is true, false or null; null always comes with a `reason`.

    A case with ``store`` runs under a lease coordinator of the unit's own (``store.py``), which stops when the
    unit ends, whatever it raises.
    """
    with store_unit(root, arm, arm_env) if case.get("store") else nullcontext() as store:
        return play_unit(
            case, arm, root, store, judge=judge, actor=actor, arm_env=arm_env, **session
        )


def play_unit(
    case: dict,
    arm: Path,
    root: Path,
    store: StoreUnit | None,
    *,
    judge: Judge,
    actor: str,
    arm_env: Mapping[str, str] | None,
    **session,
) -> dict:
    """Seed the unit's vault, play its session and score it (``run_unit``, once its coordinator runs)."""
    seeded, record, work = seed_unit(case, arm, root, arm_env, store)
    if store:
        # Every process of the unit from here serves or reads the store, so each carries the lease settings.
        arm_env = store.environment(arm_env)
    record["actor"] = actor
    # What the transport needs of the arm to withhold one write's acknowledgement, only for a case that asks.
    withholding = (
        {"withhold": run_arm_side(arm, root, "withhold", arm_env=arm_env)}
        if case.get("withhold_first_write_ack")
        else {}
    )
    played = PLAYERS[actor](
        case, arm, root, work, record, arm_env=arm_env, **withholding, **session
    )
    if played["fault"]:
        return {**record, "passed": None, "reason": f"harness fault: {played['fault']}"}
    vault = str(root / "vault")
    rule, checks, reasons, extra = case["pass"], {}, [], dict(played.get("extra", {}))
    # `seed_corrected` reads one note, so a list seed (which refuses it) has no single note to read back.
    if isinstance(seeded, dict):
        after = run_arm_side(arm, root, "probe", vault, seeded["path"], arm_env=arm_env)
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
        listed = run_arm_side(arm, root, "records", vault, *collection_paths(rule), arm_env=arm_env)
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
    if "no_write" in rule:
        writers = run_arm_side(arm, root, "write_tools", arm_env=arm_env)["tools"]
        checks["no_write"] = no_write(played["calls"], writers)
    if "pending_reconciled" in rule:
        wire = json.loads((root / "wire.json").read_text())
        held = next((call for call in wire if call["withheld"]), None)
        named = held and held["arguments"].get("collection")
        # The arm's own discovery names the collection's natural key, which tells another row from the withheld one.
        natural_key = next(
            (
                found["natural_key"]
                for found in run_arm_side(arm, root, "collections", vault, arm_env=arm_env)["collections"]
                if named in (found["title"], found["collection_id"], found["path"])
            ),
            None,
        ) if named else None
        outcome = pending_outcome(wire, withholding["withhold"]["writers"], natural_key)
        # Which of the two the agent did, and the write that was withheld; the real result is in `wire.json`.
        extra["pending_outcome"] = outcome
        extra["withheld_write"] = held and {
            key: held[key] for key in ("seq", "tool", "arguments", "identity")
        }
        checks["pending_reconciled"] = pending_reconciled(outcome)
        if checks["pending_reconciled"] is None:
            reasons.append(
                "the write that was withheld names no collection, so there was nothing to recover on"
            )
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
    if reads_collections(case):
        # The vault as the whole unit leaves it, the follow-up session included.
        vault_collections = run_arm_side(arm, root, "collections", vault, arm_env=arm_env)
        before = record["collections_before"]
        if "new_collection" in rule:
            checks["new_collection"] = new_collection(
                vault_collections, before, rule["new_collection"]
            )
            if checks["new_collection"] is None:
                reasons.append("the arm could not list the collections, or read a new one's rows")
        if "no_new_collection" in rule:
            checks["no_new_collection"] = no_new_collection(vault_collections, before)
            if checks["no_new_collection"] is None:
                reasons.append("the arm could not list the collections")
        if "error" not in vault_collections:
            created = [
                {
                    "path": c["path"],
                    "title": c["title"],
                    "natural_key": c["natural_key"],
                    "fields": c["fields"],
                    "rows": c["rows"],
                    # Recorded and never gated on: an arm without the flag compares on the outcome.
                    "automatic_in_inventory": c["automatic"],
                    "automatic_in_receipt": receipt_says_automatic(played["calls"], c),
                }
                for c in gained_collections(vault_collections, before)
            ]
            extra["new_collections"] = created
            if "new_collection" in rule:
                committed = {
                    **(committed or {}),
                    # The judge reads the schema beside the rows: the key decides what one row stands for.
                    **{
                        c["kb_path"] or c["path"]: {
                            "natural_key": c["natural_key"],
                            "fields": c["fields"],
                            "rows": c["rows"],
                        }
                        for c in gained_collections(vault_collections, before)
                        if isinstance(c["rows"], list)
                    },
                }
    if "sources_before" in record:
        facts = run_arm_side(arm, root, "sources", vault, arm_env=arm_env)
        saved = session_sources(facts, record["sources_before"])
        extra["session_sources"] = saved
        if "sources_added" in rule:
            checks["sources_added"] = sources_added(
                facts, record["sources_before"], rule["sources_added"]
            )
        for name in SOURCE_CHECKS:
            if name in rule:
                checks[name] = SOURCE_CHECK_FUNCTIONS[name](facts, record["fixtures"], rule[name])
                if checks[name] is None:
                    reasons.append(UNKNOWN_REASONS[name])
    if "judge" in rule:
        answers = played.get("answers") or [played["answer"]]
        turn, text = transcript(user_turns(case), answers)
        # The numbered transcript of several turns is never empty, so the last answer decides "no answer".
        checks["judge"], reason = judge_answer(
            case,
            text if answers[-1] else "",
            root,
            judge,
            saved,
            committed,
            calls=played["calls"],
            turn=turn,
        )
        reasons += [reason] if reason else []
    if "follow_up_judge" in rule:
        follow_up = played["follow_up"]
        extra["follow_up"] = {"tools": follow_up["tools"], "calls": follow_up["calls"]}
        checks["follow_up_judge"], reason = judge_answer(
            case,
            follow_up["answer"],
            root,
            judge,
            turn=case["follow_up"],
            name="follow_up_judge",
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
    case: dict,
    arm: Path,
    root: Path,
    work: Path,
    record: dict,
    *,
    model: str,
    max_turns: int,
    arm_env: Mapping[str, str] | None = None,
    withhold: dict | None = None,
) -> dict:
    """Run one OpenAI Agents SDK session on the seeded vault (``openai_actor.py``), as `play_claude` returns it."""
    return openai_actor.play(
        case,
        arm,
        root,
        record,
        server_instructions=run_arm_side(arm, root, "instructions", arm_env=arm_env)["server"],
        model=model,
        max_turns=max_turns,
        timeout=SESSION_TIMEOUT_SECONDS,
        arm_env=arm_env,
        withhold=withhold,
    )


#: The session drivers by actor name; each returns the session as `play_claude` documents.
PLAYERS = {"claude": play_claude, "claude-hookless": partial(play_claude, hooks=False), "openai": play_openai}


def parse_arm_env(items: list[str], arms: dict[str, Path]) -> dict[str, dict[str, str]]:
    """Each arm's own settings from ``NAME=KEY=VALUE`` items; an arm with none gets an empty mapping.

    Refuses a name that is no arm, a repeated key, and a key that is not an Exomem setting or that the
    unit's isolation already pins. The flag switches an arm's product behaviour (the typed graph, say). It
    never changes credentials, billing or PATH, and never points an arm at another vault or state root.
    """
    # The keys the runner sets for every arm, read from the code that sets them so no second list can drift.
    pinned = set(server_pins(Path())) | set(hook_env(Path()))
    settings: dict[str, dict[str, str]] = {name: {} for name in arms}
    for item in items:
        name, _, setting = item.partition("=")
        key, equals, value = setting.partition("=")
        if name not in arms:
            raise SystemExit(f"--arm-env {item}: no arm {name!r}; the arms are {sorted(arms)}")
        if not equals or not key.startswith("EXOMEM_"):
            raise SystemExit(
                f"--arm-env {item}: write NAME=KEY=VALUE, with a KEY that starts with EXOMEM_"
            )
        if key in pinned:
            raise SystemExit(
                f"--arm-env {item}: the runner's isolation already sets {key} for every arm"
            )
        if key in settings[name]:
            raise SystemExit(f"--arm-env {item}: {key} is already set for arm {name!r}")
        settings[name][key] = value
    return settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m membench.outcome.incidents")
    parser.add_argument("--arm", action="append", required=True, help="NAME=CHECKOUT, repeatable")
    parser.add_argument(
        "--arm-env",
        action="append",
        default=[],
        help="NAME=KEY=VALUE: set the Exomem setting KEY for arm NAME only, repeatable",
    )
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
        help=f"default: {INCIDENT_MAX_TURNS} (claude), {openai_actor.MAX_TURNS} (openai)",
    )
    parser.add_argument("--run-root", type=Path, required=True)
    # A held-out set lives outside the repository, so no prompt, fixture or iterating session sees it.
    parser.add_argument("--cases-dir", type=Path, default=None, help="directory of case files")
    args = parser.parse_args(argv)
    claude = args.actor != "openai"
    model = args.model or (ACTOR_MODEL if claude else openai_actor.OPENAI_MODEL)
    max_turns = args.max_turns or (INCIDENT_MAX_TURNS if claude else openai_actor.MAX_TURNS)
    if args.actor == "openai":
        openai_actor.require_sdk(os.environ)
    arms = {name: Path(path).resolve() for name, path in (item.split("=", 1) for item in args.arm)}
    for name, path in arms.items():
        if (
            not (path / "src" / "exomem").is_dir()
            or not (path / "plugins" / "claude-code").is_dir()
        ):
            raise SystemExit(f"--arm {name}: {path} holds no src/exomem and plugins/claude-code")
    arm_envs = parse_arm_env(args.arm_env, arms)
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
            name: {"checkout": str(path), **checkout_state(path), "env": arm_envs[name]}
            for name, path in arms.items()
        },
    }
    (run_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    session = {"model": model, "max_turns": max_turns}
    if claude:
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
                        arm_env=arm_envs[name],
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
