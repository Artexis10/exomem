"""The organisation replay adapter: the behaviours whose miss would corrupt a run.

Each test names the failure only it catches. Every situation value is an
invented placeholder in the shape of SCHEMA.md's toy example. The end-to-end
check is the adapter's dry run, which drives real product doors through a
scripted client; these tests cover the custody and record rules beneath it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
import yaml
from epistemic.journeys.f27_replay import AgentEnvelope
from membench.utility.organisation_replay import runner
from membench.utility.organisation_replay.__main__ import main as replay_main
from membench.utility.organisation_replay.readback import edge_review_reasons
from membench.utility.organisation_replay.runfile import call_records, check_run, read_jsonl
from membench.utility.organisation_replay.runner import RunConfig, bind, refuse_api_keys
from membench.utility.organisation_replay.scripted import DoorFault, ScriptedSession, retry_after
from membench.utility.organisation_replay.seed import (
    SeedBuild,
    check_seed,
    note_body,
    seed_session,
)
from membench.utility.organisation_replay.situation import SituationRefused, load_situation
from membench.utility.switch_eval.arms import arm_specs
from membench.utility.switch_eval.session import (
    API_KEY_VARIABLES,
    CLIENT_START,
    SERVER_START,
    AttemptResult,
    RunPaused,
    classify,
)
from membench.utility.switch_eval.state import Slot, _child_env
from membench.utility.switch_eval.tap import process_start_ticks

TOY = {
    "id": "x01",
    "pool": "development",
    "use": "test fixture; invented placeholder values",
    "category": "freshness",
    "variant": "promoted_entity_type",
    "client_shape": "hookless",
    "seed": {
        "vocabulary": [
            {"registry": "domains", "key": "topic-t"},
            {
                "registry": "relations",
                "key": "relation-r",
                "label": "relation-r",
                "parent": "relates_to",
            },
            {
                "registry": "entity-types",
                "key": "type-x",
                "label": "Type X",
                "parent": None,
                "seeded_as": "recent_promotion_by_prior_agent_session",
            },
        ],
        "homes": [{"folder": "Notes/Home H", "topics": ["topic-t"], "projects": []}],
        "entities": [{"ref": "A", "title": "Entity A", "type": "concept", "body": "Placeholder."}],
        "notes": [],
    },
    "capture_session": {"turns": ["Placeholder turn."]},
    "fresh_session": {"question": "Placeholder question?"},
}


def write_situation(directory: Path, name: str, data: dict) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_a_case_file_that_carries_a_key_is_refused(tmp_path):
    # Failure caught: a scorer key reaching the actor, the tap or a run file.
    path = write_situation(tmp_path, "x01.yaml", {**TOY, "key": {"expected": "placeholder"}})

    with pytest.raises(SituationRefused, match="carries a key"):
        load_situation(path)


@pytest.mark.parametrize(
    "extra",
    [
        {"expected_answer": "placeholder"},
        {
            "seed": {
                **TOY["seed"],
                "notes": [
                    {
                        "title": "Note N",
                        "type": "pattern",
                        "folder": "Notes/Home H",
                        "body": "Placeholder body.",
                        "supersedes": "Note M",
                    }
                ],
            }
        },
        {"seed": {**TOY["seed"], "edges": []}},
        {"seed": {**TOY["seed"], "entities": [{**TOY["seed"]["entities"][0], "aliases": []}]}},
        {"fresh_session": {"question": "Placeholder question?", "hint": "placeholder"}},
    ],
    ids=["a top-level field", "a note field", "a seed field", "an entity field", "a fresh field"],
)
def test_a_case_file_with_a_field_outside_the_schema_is_refused(tmp_path, extra):
    # Failure caught: the runner silently dropping a field the case relies on, such
    # as a supersession link, and building a different seed than the case describes.
    path = write_situation(tmp_path, "x01.yaml", {**TOY, **extra})

    with pytest.raises(SituationRefused, match="outside SCHEMA.md"):
        load_situation(path)


@pytest.mark.parametrize("name", ["dk01.yaml", "k01.yaml"])
def test_a_key_file_is_refused_by_name_before_it_is_opened(tmp_path, name):
    # Failure caught: the runner opening a key file. The path does not exist,
    # so any read would raise FileNotFoundError instead of the refusal.
    with pytest.raises(SituationRefused, match="not a situation file name"):
        load_situation(tmp_path / name)


def test_a_held_out_file_without_the_custody_manifest_is_refused_before_it_is_opened(tmp_path):
    # Failure caught: a held-out situation run without its digest check.
    with pytest.raises(SituationRefused, match="needs the custody manifest"):
        load_situation(tmp_path / "h01.yaml")


def test_a_held_out_file_under_a_manifest_other_than_the_preregistered_one_is_refused(tmp_path):
    # Failure caught: a held-out run on a partial or edited manifest that still lists the file.
    path = write_situation(tmp_path, "h01.yaml", {**TOY, "id": "h01", "pool": "heldout"})
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  h01.yaml\n", encoding="utf-8"
    )

    with pytest.raises(SituationRefused, match="not f34's preregistered held-out manifest"):
        load_situation(path, manifest=manifest)


def test_a_situation_whose_bytes_differ_from_the_manifest_is_refused(tmp_path):
    # Failure caught: a run on a situation file that changed after custody.
    path = write_situation(tmp_path, "x01.yaml", TOY)
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  x01.yaml\n", encoding="utf-8"
    )
    assert (
        load_situation(path, manifest=manifest).sha256
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )

    write_situation(
        tmp_path, "x01.yaml", {**TOY, "fresh_session": {"question": "Another question?"}}
    )

    with pytest.raises(SituationRefused, match="does not match the manifest"):
        load_situation(path, manifest=manifest)


def seed_facts(*, seed_state: str = "dismissed", prior_state: str = "open") -> dict:
    """Readback facts for TOY's seed as the product reports them after the settle session."""

    return {
        "registries": {
            "domains": ["topic-t"],
            "projects": [],
            "entity_types": {"core": ["concept"], "vault": {"type-x": {"parent_chain": []}}},
            # The product saves a proposed relation under its own canonical key.
            "relations": {"vault.relation_r": {"parent_chain": ["relates_to"]}},
            "homes": {"home-h": {"folder": "Notes/Home H", "topics": ["topic-t"], "projects": []}},
        },
        "pages": [{"path": "Knowledge Base/Entities/Concept/entity-a.md", "title": "Entity A"}],
        "promotions": [
            {
                "id": "p1",
                "state": seed_state,
                "subjects": [{"registry": "relations", "key": "vault.relation_r"}],
            },
            {
                "id": "p2",
                "state": prior_state,
                "subjects": [{"registry": "entity-types", "key": "type-x"}],
            },
        ],
    }


def seed_build() -> SeedBuild:
    return SeedBuild(
        relation_keys={"relation-r": "vault.relation_r"},
        home_keys={"home-h": "Notes/Home H"},
        entities=[
            {"ref": "A", "title": "Entity A", "path": "Knowledge Base/Entities/Concept/entity-a.md"}
        ],
        prior_entries=[{"registry": "entity-types", "key": "type-x", "saved_as": "type-x"}],
    )


def test_a_seed_read_back_through_canonical_keys_passes_its_check(tmp_path):
    # Failure caught: a correct seed struck because the product renamed a relation key.
    situation = load_situation(write_situation(tmp_path, "x01.yaml", TOY))

    assert check_seed(situation, seed_build(), seed_facts()) == []


@pytest.mark.parametrize(
    ("states", "problem"),
    [
        ({"seed_state": "open"}, "seed promotion p1 is still open"),
        ({"prior_state": "dismissed"}, "prior-agent promotion p2 is dismissed"),
    ],
)
def test_the_seed_check_holds_promotions_to_their_seeded_state(tmp_path, states, problem):
    # Failure caught: established seed state shown to the capture session as a
    # fresh promotion, or the prior agent's promotion gone before it starts.
    situation = load_situation(write_situation(tmp_path, "x01.yaml", TOY))

    assert check_seed(situation, seed_build(), seed_facts(**states)) == [problem]


def test_call_records_tell_a_preview_from_a_write_and_read_product_refusals():
    # Failure caught: a validate-only preview counted as a write, or a product
    # refusal (an MCP success carrying a structured error) counted as served.
    def answer(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": False}

    events = [
        {
            "event": "tools_list",
            "tools": [{"name": "create_note", "annotations": {"readOnlyHint": False}}],
        },
        {
            "event": "call",
            "turn": 0,
            "ordinal": 1,
            "name": "create_note",
            "outcome": "served",
            "arguments": {"title": "T", "validate_only": True},
            "result": answer(
                '{"destination": "Knowledge Base/Notes/Patterns/t.md", "draft_hash": "d1"}'
            ),
        },
        {
            "event": "call",
            "turn": 0,
            "ordinal": 2,
            "name": "create_note",
            "outcome": "served",
            "arguments": {"title": "T", "draft_hash": "d1"},
            "result": answer('{"success": false, "error": {"code": "STALE_DRAFT"}}'),
        },
        {
            "event": "call",
            "turn": 0,
            "ordinal": 3,
            "name": "create_note",
            "outcome": "served",
            "arguments": {"title": "T", "draft_hash": "d2"},
            "result": answer('{"ok": true, "path": "Knowledge Base/Notes/Patterns/t.md"}'),
        },
        {
            "event": "call",
            "turn": 0,
            "ordinal": 4,
            "name": "create_note",
            "outcome": "refused_budget",
            "arguments": {"title": "T"},
            "result": None,
        },
    ]

    records = call_records(events)

    assert [(r["mutating"], r["refusal_code"]) for r in records] == [
        (False, None),
        (True, "STALE_DRAFT"),
        (True, None),
        (True, "refused_budget"),
    ]
    assert records[1]["retry_hash"] == "d1"
    assert records[2]["target"] == {"path": "Knowledge Base/Notes/Patterns/t.md", "title": "T"}


def conforming_run(run_dir: Path) -> dict:
    (run_dir / "t.jsonl").write_text("", encoding="utf-8")
    session = {
        "session_id": "s",
        "attempt": 1,
        "init": {},
        "transcript_path": "t.jsonl",
        "tap_log_path": "t.jsonl",
        "calls": [],
        "final_answer": "a",
        "turn_results": [],
        "guard_refusals": 0,
    }
    return {
        "situation_id": "x01",
        "situation_sha256": "0" * 64,
        "arm": "arm-a",
        "client_shape": "hookless",
        "binding_ref": "b",
        "seed": {
            "tree_digest": "t",
            "projection_digest": "p",
            "registry_overlay_digests": {},
            "check": "pass",
            "settled_promotion_ids": [],
        },
        "sessions": [{**session, "role": "capture"}, {**session, "role": "fresh"}],
        "end_state": {
            "pages": [],
            "pages_changed": [],
            "edges": [],
            "entity_types": {},
            "relations": {},
            "registry_history": [],
            "homes": {},
            "operation_log": {"edge_review_reasons": {"status": "unavailable"}},
            "promotions": [],
        },
        "faults": [],
        "status": "ok",
    }


def test_the_run_check_rejects_a_run_without_its_fresh_session(tmp_path):
    # Failure caught: the scorer reading a run that lost its scored session as complete.
    run = conforming_run(tmp_path)
    assert check_run(run, tmp_path) == []

    run["sessions"] = run["sessions"][:1]

    assert check_run(run, tmp_path) == ["sessions are ['capture']"]


def test_an_api_key_in_the_environment_refuses_the_run():
    # Failure caught: the actor billed to an API key instead of the subscription.
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        refuse_api_keys({"ANTHROPIC_API_KEY": "placeholder"})


def test_a_plain_seed_body_is_wrapped_verbatim_under_one_heading_the_registry_accepts(tmp_path):
    # Failure caught: create_note refusing a plain body (missing_semantic_unit) and
    # striking the situation, or a wrap that changes the body's text.
    body, heading = note_body("Placeholder body.", "pattern", tmp_path)

    assert heading is not None
    assert body == f"## {heading}\n\nPlaceholder body."
    assert note_body(body, "pattern", tmp_path) == (body, None)


def test_a_hook_the_arm_wires_passes_and_any_other_hook_still_stops_the_run():
    # Failure caught: the shipped hook set's Stop hook aborting every hooked
    # session as a surface leak, or a hook outside that set passing unnoticed.
    spec = dataclasses.replace(arm_specs()["A"], wired_hooks=frozenset({"Stop"}))

    def outcome(hook_event: str | None) -> tuple[str, str]:
        events = [
            {"type": "system", "subtype": "hook_started", "hook_id": "h1", "hook_event": hook_event}
        ]
        result = classify(
            spec=spec, model="m", client_version="v", events=events if hook_event else [],
            turn_records=[], tap_events=[], scored_turn=0, timed_out=False, quota=None,
            surface=None,
        )  # fmt: skip
        return result.outcome, result.kind

    assert outcome("Stop") == outcome(None)
    assert outcome("PreToolUse") == ("abort", "surface_hooks")


@pytest.mark.parametrize(
    ("status", "committed", "expected"),
    [
        pytest.param("retryable", False, 0.75, id="uncommitted warm-up refusal is resent"),
        pytest.param("retryable", None, None, id="a refusal of unknown commit is final"),
        pytest.param("conflict", False, None, id="a non-retryable refusal is final"),
    ],
)
def test_the_seed_resends_only_a_refusal_that_committed_nothing(status, committed, expected):
    # Failure caught: a seed write resent after a refusal that may have
    # committed, writing the seed twice, or a final refusal retried until timeout.
    error = {"code": "MUTATION_WARMING", "status": status, "retry_after_ms": 750}
    if committed is not None:
        error["committed"] = committed

    assert retry_after({"success": False, "error": error}) == expected


def product_slot(tmp_path):
    """A fresh slot whose vault this checkout's product initialised, its arm spec and env.

    A test that uses it starts the real product server, so it gets a longer timeout.
    """
    config = RunConfig(
        out=tmp_path, arm="arm-x", profile="neutral-lexical", model="m",
        client=AgentEnvelope(Path("claude"), "1.0.0 (Claude Code)"), launcher=(),
        revision="a" * 40, base_revision="a" * 40, wheel=None,
    )  # fmt: skip
    spec = runner.arm_spec(config, "hookless", ())
    slot = Slot(tmp_path / "slot")
    runner.fresh_slot(slot, spec)
    return slot, spec, _child_env(os.environ, slot, spec)


@pytest.mark.timeout(180)
def test_a_refusal_inside_a_wrapped_answer_is_a_refusal_to_the_seed_and_the_run_file(tmp_path):
    # Failure caught: connect_memory answering as {"result": ...}, so its refusal
    # (a warm-up refusal included) reads as success: the seed records an entity
    # at no page without resending the call, and a measured call has no refusal code.
    slot, spec, env = product_slot(tmp_path)

    async def create_entity_of_unregistered_type():
        async with ScriptedSession(slot, spec, env=env, log_dir=tmp_path / "log", name="seed") as s:
            with pytest.raises(DoorFault, match="ENTITY_TYPE_UNKNOWN"):
                await s.call(
                    "connect_memory",
                    {"operation": "create-entity", "entity_type": "type-unregistered",
                     "name": "Entity Z", "summary": "Placeholder."},
                )  # fmt: skip
            return s.tap_log

    records = call_records(read_jsonl(asyncio.run(create_entity_of_unregistered_type())))

    assert records[-1]["refusal_code"] == "ENTITY_TYPE_UNKNOWN"


@pytest.mark.timeout(180)
def test_seed_notes_in_a_fresh_vault_commit_whether_or_not_the_draft_asks_for_a_review(tmp_path):
    # Failure caught: the seed sending a reviewed-none review for a fresh vault's
    # first note, which bootstraps without one, so the product refuses its commit
    # (INVALID_RELATION_REVIEW) and every situation without a seed entity strikes.
    notes = [
        {"title": title, "type": "pattern", "folder": "Notes/Home H", "topics": ["topic-t"],
         "body": f"## Decision\n\nPlaceholder decision for {title}.\n"}
        for title in ("Note M", "Note N")
    ]  # fmt: skip
    # The notes' home and its topic only: no seed entity, so the first note is the vault's first page.
    vocabulary = [{"registry": "domains", "key": "topic-t"}]
    toy = {**TOY, "seed": {**TOY["seed"], "vocabulary": vocabulary, "entities": [], "notes": notes}}
    situation = load_situation(write_situation(tmp_path, "x01.yaml", toy))
    slot, spec, env = product_slot(tmp_path)
    build = SeedBuild()

    async def seed():
        async with ScriptedSession(slot, spec, env=env, log_dir=tmp_path / "log", name="seed") as s:
            await seed_session(s, situation, build, slot.vault)
            return s.tap_log

    drafts = [
        event["result"]["structuredContent"]
        for event in read_jsonl(asyncio.run(seed()))
        if event.get("name") == "create_note" and event["arguments"].get("validate_only")
    ]

    # The first note bootstraps the vault; the second one's draft asks for the review.
    assert [draft["reviewed_none_required"] for draft in drafts] == [False, True]
    assert [note["title"] for note in build.notes if note["landed"]] == ["Note M", "Note N"]


@pytest.mark.parametrize(
    ("reason_capable", "expected"),
    [
        pytest.param(True, ("available", 0), id="a capable arm that recorded none measures zero"),
        pytest.param(False, ("unavailable", None), id="an incapable arm stays unavailable"),
    ],
)
def test_edge_review_reasons_follow_the_bound_capability_not_the_log(reason_capable, expected):
    # Failure caught: a candidate that kept a generic edge without a reason reported
    # as unavailable, or the baseline's hard-coded false flag reported as a measured zero.
    edge = {"generic": True, "reason": None, "reason_recorded": False}
    entries = [{"provenance": {"added_edges": [edge]}}]

    reasons = edge_review_reasons(entries, reason_capable=reason_capable)

    assert (reasons["status"], reasons["reasons_recorded"]) == expected
    assert reasons["generic_added_edges"] == 1


def test_a_candidate_bound_at_the_base_revision_is_refused(tmp_path):
    # Failure caught: the baseline bound as the candidate, so its writer's hard-coded
    # false flags would read as a capable arm's measured zero reasons.
    base = "a" * 40
    config = RunConfig(
        out=tmp_path, arm="arm-x", profile="neutral-lexical", model="m", client=None,
        launcher=(), revision=base, base_revision=base, wheel=None, role="candidate",
    )  # fmt: skip

    with pytest.raises(SystemExit, match="candidate arm"):
        bind(config)


def test_a_candidate_that_changes_a_shipped_hook_still_pairs_with_the_baseline():
    def binding(hook_digest):
        return {
            "base_revision": "a" * 40,
            "profile": "neutral-lexical",
            "client": {"model": "m", "version": "1.0.0", "standin": False, "launcher": []},
            "shapes": {"hooked": {"hook_scripts_sha256": hook_digest}},
        }

    assert runner.arm_parity(binding("1" * 64)) == runner.arm_parity(binding("2" * 64))


class Crash(BaseException):
    """The runner process dying: nothing in the runner catches it."""


def stub_runner(tmp_path, monkeypatch, execute, readback=None, ids=("x01",)):
    """Toy situations on stubs: a seed that writes a page, an install that marks a
    hook restart pending, ``execute(inputs, attempt_dir, slot)`` as the client and
    ``readback()`` as the end-state readback. ``run()`` reruns the first situation;
    ``run.cli(*flags)`` runs the ``run`` command over all of them."""

    for name in API_KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    seed_builds = []

    def build_seed(config, situation, slot, spec, run_dir, faults):
        seed_builds.append(slot)
        for path in (slot.vault, slot.state, slot.runtime, slot.cwd, slot.hook_home):
            path.mkdir(parents=True)
        # A real seed build writes new ids and times, so each build's bytes differ.
        (slot.vault / "seed.md").write_text(f"seed build {len(seed_builds)}", encoding="utf-8")
        facts = {"projection_digest": "p", "registries": {"overlay_digests": {}}}
        return SeedBuild(), {**facts, "promotions": [], "pages": []}

    def session_files(slot, spec, files_dir, *, hooked):
        files_dir.mkdir(parents=True, exist_ok=True)
        (files_dir / "mcp.json").write_text("{}", encoding="utf-8")
        (slot.hook_home / "restart-pending").write_text("pending", encoding="utf-8")
        return {"mcp.json": files_dir / "mcp.json"}, frozenset()

    class Executor:
        def __init__(self, **_):
            pass

        def execute_prepared(self, inputs, attempt_dir, slot):
            attempt_dir.mkdir(parents=True, exist_ok=True)
            return execute(inputs, attempt_dir, slot)

    monkeypatch.setattr(runner, "_build_seed", build_seed)
    monkeypatch.setattr(runner, "session_files", session_files)
    monkeypatch.setattr(runner, "files_digests", lambda paths, slot: {})
    monkeypatch.setattr(runner, "ClaudeSessionExecutor", Executor)
    monkeypatch.setattr(runner, "_child", lambda *args: (readback or (lambda: {"read": {}}))())
    files = [write_situation(tmp_path, f"{id_}.yaml", {**TOY, "id": id_}) for id_ in ids]
    situation = load_situation(files[0])
    config = RunConfig(
        out=tmp_path / "out", arm="arm-x", profile="neutral-lexical", model="m",
        client=AgentEnvelope(Path("claude"), "1.0.0 (Claude Code)"), launcher=(),
        revision="a" * 40, base_revision="a" * 40, wheel=None,
    )  # fmt: skip
    binding = {
        "binding_ref": "arm-x-aaaaaaaaaaaa",
        "arm": "arm-x",
        "revision": "a" * 40,
        "base_revision": "a" * 40,
        "profile": "neutral-lexical",
        "client": {"executable": "claude", "version": "1.0.0", "launcher": [], "model": "m",
                   "standin": True},
        "reason_capable": False,
        "shapes": {"hookless": {"tools": [], "wired_hooks": [], "surface": {}}},
    }  # fmt: skip
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "binding-arm-x.json").write_text(json.dumps(binding), encoding="utf-8")

    def run():
        path = runner.run_situation(config, binding, situation)
        return json.loads(path.read_text(encoding="utf-8"))

    def cli(*flags):
        situations = [arg for path in files for arg in ("--situation", str(path))]
        return replay_main(["run", "--out", str(tmp_path / "out"), "--arm", "arm-x", *situations, *flags])  # fmt: skip

    run.seed_builds = seed_builds
    run.cli = cli
    return run


def ended(pid):
    """Gone, or a zombie that no longer runs."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "Z"
    except FileNotFoundError:
        return True


def run_file(tmp_path, id_):
    return json.loads((tmp_path / "out" / id_ / "arm-x" / "run.json").read_text(encoding="utf-8"))


def test_a_situation_paused_on_a_quota_resumes_where_it_stopped(tmp_path, monkeypatch):
    # Failure caught: a quota pause recorded as a struck situation, or a rerun that
    # rebuilds the seed under a finished capture session and aborts on its inputs.
    attempts = []

    def execute(inputs, attempt_dir, slot):
        attempts.append(inputs.tree)
        if inputs.tree == "capture":
            (slot.vault / "captured.md").write_text("capture write", encoding="utf-8")
            return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "noted"}])
        # The fresh session starts on the capture session's end state.
        assert (slot.vault / "captured.md").exists()
        if attempts.count("fresh") == 1:
            return AttemptResult(outcome="quota", resets_at="later")
        return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "recalled"}])

    run = stub_runner(tmp_path, monkeypatch, execute)

    with pytest.raises(RunPaused):
        run()
    assert not (tmp_path / "out" / "x01" / "arm-x" / "run.json").exists()

    result = run()

    assert result["status"] == "ok"
    assert attempts == ["capture", "fresh", "fresh"]
    assert len(run.seed_builds) == 1
    assert [(f["session"], f["kind"]) for f in result["faults"]] == [("fresh", "quota")]
    assert [s["final_answer"] for s in result["sessions"]] == ["noted", "recalled"]


def test_a_rerun_skips_a_finished_situation_instead_of_refusing_the_whole_run(
    tmp_path, monkeypatch
):
    # Failure caught: a multi-situation rerun stopping at the first finished situation,
    # so it never reaches the paused one after it.
    attempts = []

    def execute(inputs, attempt_dir, slot):
        attempts.append(inputs.tree)
        return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "fine"}])

    run = stub_runner(tmp_path, monkeypatch, execute)
    first = run()

    assert run() == first
    assert attempts == ["capture", "fresh"]


def test_a_runner_killed_mid_attempt_ends_its_client_and_server_and_keeps_that_attempt(
    tmp_path, monkeypatch
):
    # Failure caught: a resumed attempt overwriting the killed draw's transcript and
    # not counting it, or the dead runner's client and server writing into the restored slot.
    attempts = []
    client = {}

    def execute(inputs, attempt_dir, slot):
        attempts.append((inputs.tree, attempt_dir.name))
        if len(attempts) == 1:
            (attempt_dir / "transcript.jsonl").write_text("the killed draw", encoding="utf-8")
            # As the executor starts it: a session of its own, in the slot's directory,
            # with its server as a child.
            client["process"] = subprocess.Popen(
                ["sh", "-c", "sleep 30 & echo $!; wait"],  # noqa: S607 - a stand-in client
                cwd=slot.cwd, start_new_session=True, stdout=subprocess.PIPE, text=True,
            )  # fmt: skip
            client["server"] = int(client["process"].stdout.readline())
            started = {"event": "server_started", "pid": client["server"]}
            started["start_ticks"] = process_start_ticks(client["server"])
            (slot.runtime / "tap.jsonl").write_text(json.dumps(started) + "\n", encoding="utf-8")
            raise Crash
        return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "fine"}])

    run = stub_runner(tmp_path, monkeypatch, execute)
    try:
        with pytest.raises(Crash):
            run()
        result = run()
        killed = client["process"].wait(timeout=5)
    finally:
        client["process"].kill()

    capture = tmp_path / "out" / "x01" / "arm-x" / "sessions" / "capture"
    assert killed == -signal.SIGKILL
    assert ended(client["server"])
    assert (capture / "attempt-1" / "transcript.jsonl").read_text() == "the killed draw"
    assert (capture / "attempt-1" / "runtime" / "tap.jsonl").exists()
    assert attempts[1:] == [("capture", "attempt-2"), ("fresh", "attempt-1")]
    assert [(f["session"], f["attempt"], f["kind"]) for f in result["faults"]] == [
        ("capture", 1, "harness_fault")
    ]
    assert result["status"] == "ok"


def test_a_resume_after_capture_shows_the_fresh_session_the_hook_state_capture_left(
    tmp_path, monkeypatch
):
    # Failure caught: a resume reinstalling the hooks, so the fresh session meets a
    # restart-pending marker that an uninterrupted run's fresh session never sees.
    seen = []
    readbacks = []

    def execute(inputs, attempt_dir, slot):
        marker = slot.hook_home / "restart-pending"
        if inputs.tree == "capture":
            marker.write_text("consumed", encoding="utf-8")
        else:
            seen.append(marker.read_text(encoding="utf-8"))
        return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "fine"}])

    def readback():
        readbacks.append(1)
        if len(readbacks) == 1:
            raise Crash
        return {"read": {}}

    run = stub_runner(tmp_path, monkeypatch, execute, readback)
    with pytest.raises(Crash):
        run()

    assert run()["status"] == "ok"
    assert seen == ["consumed"]


def test_an_environment_wide_start_fault_aborts_the_run_at_the_first_situation(
    tmp_path, monkeypatch
):
    # Failure caught: a client that never starts striking every situation in turn,
    # which voids an f34 run once more than two are struck.
    attempts = []

    def execute(inputs, attempt_dir, slot):
        attempts.append(inputs.planned.case_id)
        return AttemptResult(outcome="harness_fault", kind=CLIENT_START, detail="no init event")

    run = stub_runner(tmp_path, monkeypatch, execute, ids=("x01", "x02", "x03", "x04"))

    assert run.cli() == 2
    assert attempts == ["x01", "x01", "x01"]
    assert not list((tmp_path / "out").glob("*/arm-x/run.json"))


def test_the_lane_strikes_an_aborted_situation_with_its_reason_and_the_rerun_moves_on(
    tmp_path, monkeypatch
):
    # Failure caught: a start fault that only this situation's seed causes aborting
    # every rerun, with no way to close the situation.
    def execute(inputs, attempt_dir, slot):
        if inputs.planned.case_id == "x01":
            return AttemptResult(outcome="harness_fault", kind=SERVER_START, detail="no handshake")
        return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "fine"}])

    run = stub_runner(tmp_path, monkeypatch, execute, ids=("x01", "x02"))
    assert run.cli() == 2

    assert run.cli("--strike", "x01", "--reason", "the seed's page breaks the server") == 0
    struck = run_file(tmp_path, "x01")
    assert struck["status"] == "struck"
    assert struck["interruptions"][-1]["detail"] == "the seed's page breaks the server"
    assert [(f["session"], f["attempt"]) for f in struck["faults"]] == [
        ("capture", 1),
        ("capture", 2),
        ("capture", 3),
    ]

    assert run.cli() == 0
    assert run_file(tmp_path, "x02")["status"] == "ok"


def test_a_struck_situation_stops_the_run_and_keeps_its_vault(tmp_path, monkeypatch):
    # Failure caught: one cause striking situation after situation in one run, or a
    # struck situation's vault wiped with its slot, so its fault cannot be read.
    def execute(inputs, attempt_dir, slot):
        if inputs.planned.case_id == "x01":
            (slot.vault / "half-written.md").write_text("partial", encoding="utf-8")
            return AttemptResult(outcome="harness_fault", kind="transport", detail="dropped")
        return AttemptResult(outcome="ok", turns=[{"index": 0, "answer": "fine"}])

    run = stub_runner(tmp_path, monkeypatch, execute, ids=("x01", "x02"))

    assert run.cli() == 2
    assert run_file(tmp_path, "x01")["status"] == "struck"
    assert not (tmp_path / "out" / "x02").exists()
    attempt = tmp_path / "out" / "x01" / "arm-x" / "sessions" / "capture" / "attempt-3"
    assert (attempt / "vault" / "half-written.md").read_text() == "partial"

    assert run.cli() == 0
    assert run_file(tmp_path, "x02")["status"] == "ok"
