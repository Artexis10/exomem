"""The f33 driver: two sessions per run, the fresh session's packet, faults and reruns.

Every stream here is FABRICATED, shaped after streams recorded from
``claude 2.1.296 -p --output-format stream-json --verbose --include-hook-events``
on 2026-10-11: the init line's ``memory_paths``, ``apiKeySource``,
``mcp_servers``, ``plugins``, ``model`` and ``claude_code_version``, an
``activate_context`` call answered by a JSON-text ``tool_result``, a
``UserPromptSubmit`` ``hook_response`` whose ``output`` carries
``hookSpecificOutput.additionalContext``, and the result line's
``permission_denials``. No test pins stream bytes, and names and values come
from the digest-verified answer key, never restated here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.reads_repository_docs

ROOT = Path(__file__).resolve().parents[1]
PET = "f33-pet-owner-v1"
PET_TWIN = "f33-pet-owner-twin-v1"
VERSION = "2.1.296"
MODEL = "claude-sonnet-5-5"
ACTIVATE = "mcp__exomem__activate_context"


def _driver():
    from epistemic.journeys import f33_referent_replay

    return f33_referent_replay


def _key(case_id: str) -> dict:
    from epistemic.journeys.referent_capture import case_key

    return case_key(case_id)


def _case(case_id: str):
    from epistemic.journeys.referent_capture import build_case_set

    return next(case for case in build_case_set() if case.case_id == case_id)


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _init(profile: Path, *, plugin: Path | None = None, **overrides) -> dict:
    plugins = [{"name": "cc-plugin-telemetry", "path": "builtin", "source": "x@builtin"}]
    if plugin is not None:
        plugins.insert(0, {"name": "exomem", "path": str(plugin), "source": "exomem@inline"})
    line = {
        "type": "system",
        "subtype": "init",
        "session_id": "s",
        "model": MODEL,
        "claude_code_version": VERSION,
        "apiKeySource": "none",
        "mcp_servers": [{"name": "exomem", "status": "connected", "source": "dynamic"}],
        "plugins": plugins,
        "memory_paths": {"auto": str(profile / "projects" / "-run" / "memory") + "/"},
    }
    line.update(overrides)
    return line


def _call(tool_id: str, name: str, payload: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": payload}]},
    }


def _reply(tool_id: str, content, *, is_error: bool | None = None) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _hook(event: str, context: str) -> dict:
    output = json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}})
    return {"type": "system", "subtype": "hook_response", "hook_event": event, "output": output + "\n"}


def _result(text: str = "done", *, subtype: str = "success", denials=()) -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "num_turns": 2,
        "duration_ms": 1000,
        "result": text,
        "permission_denials": list(denials),
        "usage": {"input_tokens": 2, "output_tokens": 4},
        "total_cost_usd": 0.1,
        "session_id": "s",
    }


def _stream(*lines: dict) -> str:
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def _prompt(argv: list[str]) -> str:
    return argv[argv.index("-p") + 1]


def _plugin(argv: list[str]) -> Path | None:
    return Path(argv[argv.index("--plugin-dir") + 1]) if "--plugin-dir" in argv else None


def _write_referent(vault: Path, case_id: str) -> str:
    """What a capturing agent leaves: a vault-declared type, the entity and the key edge."""

    import uuid

    key = _key(case_id)
    name = key["referent"]["name"]
    kb = vault / "Knowledge Base"
    (kb / "_Schema/entity-types.yaml").write_text(
        "schema_version: 1\nentity_types:\n  companion:\n    folder: Companions\n"
        "    label: Companion\n    aliases: []\n    cue_nouns: []\n"
        "    capture_guidance: A durable companion.\n    status: active\n",
        encoding="utf-8",
    )
    exomem_id = str(uuid.uuid4())
    page = kb / f"Entities/Companions/{name}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"---\ntype: entity\nentity_type: companion\ntitle: {name}\nstatus: active\n"
        f"exomem_id: {exomem_id}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    owner = vault / f"{key['owner']['ref']}.md"
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- owns [[Entities/Companions/{name}]]\n",
        encoding="utf-8",
    )
    return exomem_id


# The fresh session's activate_context results, built from the entity's memory ref
# and path: a list of (tool result content, is_error flag).


def _turn_packet(ref: str, path: str) -> list:
    anchor = {"ref": ref, "path": path, "status": "resolved", "origin": "turn"}
    unit = {"ref": ref, "provenance": {"path": path, "anchor": ref}}
    return [({"anchors": [anchor], "units": [unit], "pointers": []}, None)]


def _recent_only(ref: str, path: str) -> list:
    recent = {"ref": ref, "path": path, "why": "edited"}
    return [({"recent_context": [recent], "anchors": [], "units": [], "pointers": []}, None)]


def _recency_carried(ref: str, path: str) -> list:
    """A packet the edit-time prior carried: an anchor, a served unit and a pointer, all of the entity."""

    anchor = {"ref": ref, "path": path, "status": "resolved", "evidence": ["recency"], "origin": "turn"}
    unit = {"ref": ref, "provenance": {"path": path, "anchor": ref}}
    (pointer,) = _unit_pointer(ref, path)[0][0]["pointers"]
    packet = {
        "anchors": [anchor],
        "units": [unit],
        "pointers": [pointer],
        "generation": {"carried_by": "recency"},
    }
    return [(packet, None)]


def _unit_pointer(ref: str, path: str) -> list:
    """A pointer to a semantic unit of the entity page, in the product's own ref shape."""

    from exomem.semantic_units import parse_semantic_units

    (unit,) = parse_semantic_units("- [fact] A durable fact ^fact-1\n", path=path, parent_ref=ref).units
    pointer = {"ref": unit.unit_ref, "title": "fact", "why": "identity lane", "reason": "budget"}
    return [({"anchors": [], "units": [], "pointers": [pointer]}, None)]


def _error_envelope() -> dict:
    from exomem import cli_ops

    return cli_ops.envelope(False, error={"code": "OP_ERROR", "message": "lane failed"})


class _Agent:
    """An offline client: capture turns succeed, and a positive's fresh session activates.

    ``capture`` writes into the run's own vault, found through the environment the
    driver hands the child. ``faults`` makes the first N invocations exit non-zero.
    ``results`` builds the fresh session's activate_context results, ``init_for``
    overrides the init line of a given call number, and ``before`` runs first on
    every call with its argv.
    """

    def __init__(
        self, case_id: str, profile: Path, *, capture: bool = False, faults: int = 0,
        results=_turn_packet, init_for=None, before=None,
    ):
        self.case_id, self.profile, self.capture, self.faults = case_id, profile, capture, faults
        self.results, self.init_for = results, init_for or (lambda _call: {})
        self.before = before or (lambda _argv: None)
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.exomem_id: str | None = None

    def __call__(self, argv, cwd, env):
        self.before(argv)
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        init = _init(self.profile, plugin=_plugin(argv), **self.init_for(len(self.calls)))
        if len(self.calls) <= self.faults:
            return _Proc(_stream(init, _result("boom", subtype="error_during_execution")), 1)
        key = _key(self.case_id)
        if _prompt(argv) != _case(self.case_id).question:
            if self.capture and self.exomem_id is None:
                self.exomem_id = _write_referent(Path(env["EXOMEM_VAULT_PATH"]), self.case_id)
            return _Proc(_stream(init, _result()))
        from exomem.memory_refs import memory_ref

        entity = f"Knowledge Base/Entities/Companions/{key['referent']['name']}.md"
        lines = [init]
        for n, (content, is_error) in enumerate(self.results(memory_ref(self.exomem_id), entity), 1):
            text = content if isinstance(content, str) else json.dumps(content)
            lines += [_call(f"toolu_{n}", ACTIVATE, {"turn": _prompt(argv)}), _reply(f"toolu_{n}", text, is_error=is_error)]
        return _Proc(_stream(*lines, _result(f"The answer is {key['answer_value']}.")))


def _run(
    tmp_path: Path, agent: _Agent, *, case_ids, shapes=("hookless",), parent_env=None, model=MODEL, **kwargs
):
    from epistemic.journeys import f27_replay as client

    return _driver().run_driver(
        out_dir=tmp_path / "out",
        taken_at="2026-10-11T00:00:00Z",
        profile=agent.profile,
        case_ids=case_ids,
        shapes=shapes,
        envelope=client.AgentEnvelope(executable=Path("/opt/agent/claude"), version=f"{VERSION} (x)"),
        product={"head": "0" * 40},
        parent_env=parent_env or {"PATH": "/usr/bin", "HOME": str(tmp_path)},
        runner=agent,
        prominence_writer=lambda _arm, _env: "set",
        model=model,
        **kwargs,
    )


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    return profile


def _run_file(tmp_path: Path, case_id: str, shape: str = "hookless", attempt: int = 1) -> dict:
    path = tmp_path / "out" / case_id / shape / f"attempt-{attempt:02d}" / "run.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _entity_id() -> str:
    return f"Knowledge Base/Entities/Companions/{_key(PET)['referent']['name']}"


def test_the_fresh_session_packet_is_recorded_and_its_refs_map_to_snapshot_ids(tmp_path) -> None:
    """Fails when the fresh session, its packet or the ref mapping is lost from the run file."""

    agent = _Agent(PET, _profile(tmp_path), capture=True)
    result = _run(tmp_path, agent, case_ids=[PET], repeats=1)

    assert result["manifest"]["claim_status"].startswith("pending acknowledgment")
    assert json.loads((tmp_path / "out" / "report.json").read_text())["partial"] is False
    row = result["cases"][0]["runs"][0]
    assert (row["status"], row["repeat_outcome"]) == ("scored", "pass")
    run = _run_file(tmp_path, PET)
    capture, fresh = run["sessions"]["capture"], run["sessions"]["fresh"]
    assert [call["label"] for call in capture["invocations"]] == [t for t, _ in _case(PET).turns]
    assert all(call["stream"] for call in (*capture["invocations"], *fresh["invocations"]))
    assert capture["session_id"] != fresh["session_id"]
    assert "--resume" not in fresh["invocations"][0]["argv"]
    assert run["packet"]["captures"][0]["readable"] is True
    assert {ref["item_id"] for ref in run["packet"]["refs"]} == {_entity_id()}
    assert {name: row["outcome"] for name, row in run["assertions"].items()} == {
        "referent_entity_typed": "pass",
        "referent_key_edge_present": "pass",
        "referent_used_in_fresh_session": "pass",
    }
    assert [call["name"] for call in run["tool_calls"]] == [ACTIVATE]


@pytest.mark.parametrize("results", [_recent_only, _recency_carried], ids=["recent_context", "recency_carry"])
def test_a_ref_the_question_did_not_choose_is_not_served(tmp_path, results) -> None:
    """Fails when recent work, or anything in a recency-carried packet, serves (rulings 1 and 1b).

    A capture session edits the referent's page, so edit time names it whatever the
    question was; the answer here states the key value, and "uses" must still fail.
    """

    agent = _Agent(PET, _profile(tmp_path), capture=True, results=results)
    result = _run(tmp_path, agent, case_ids=[PET], repeats=1)

    rule = result["manifest"]["uses_rule"]
    assert rule["fixed_before_any_baseline"] is True and "recency_carry" in rule
    run = _run_file(tmp_path, PET)
    if results is _recency_carried:
        assert {ref["section"] for ref in run["packet"]["refs"]} >= {"anchors", "units", "pointers"}
    assert {(ref["item_id"], ref["serves"]) for ref in run["packet"]["refs"]} == {(_entity_id(), False)}
    uses = run["assertions"]["referent_used_in_fresh_session"]
    assert (uses["outcome"], "does not serve" in uses["evidence"]) == ("fail", True)


def test_a_semantic_unit_ref_serves_its_page(tmp_path) -> None:
    """Fails when a unit ref (`<memory ref>#<anchor>`) is left unmapped, so a pointer serves nothing."""

    agent = _Agent(PET, _profile(tmp_path), capture=True, results=_unit_pointer)
    _run(tmp_path, agent, case_ids=[PET], repeats=1)

    run = _run_file(tmp_path, PET)
    (ref,) = run["packet"]["refs"]
    assert ("#" in ref["ref"], ref["item_id"], ref["serves"]) == (True, _entity_id(), True)
    assert run["assertions"]["referent_used_in_fresh_session"]["outcome"] == "pass"


@pytest.mark.parametrize(
    ("results", "status", "uses"),
    [
        (lambda ref, path: [], "scored", "fail"),
        (lambda ref, path: [(_error_envelope(), None)], "blocked", None),
        (lambda ref, path: [("server error", True)], "blocked", None),
        (lambda ref, path: [(_error_envelope(), None), *_turn_packet(ref, path)], "scored", "pass"),
    ],
    ids=["no_call", "envelope_only", "is_error_only", "envelope_then_packet"],
)
def test_errored_activations_are_skipped_and_block_only_when_none_is_readable(
    tmp_path, results, status: str, uses: str | None
) -> None:
    """Fails when an error envelope scores as an empty packet, or blocks beside a readable one."""

    agent = _Agent(PET, _profile(tmp_path), capture=True, results=results)
    result = _run(tmp_path, agent, case_ids=[PET], repeats=1, max_attempts=1)

    row = result["cases"][0]["runs"][0]
    assert row["status"] == status
    assert row["assertions"].get("referent_used_in_fresh_session") == uses
    if status == "blocked":
        assert "readable packet" in row["fault_reason"]


def test_a_quiet_twin_is_scored_on_the_runtime_projection_rather_than_blocked(tmp_path) -> None:
    """Fails when a twin's notice surfaces go unprojected, which blocks every twin run."""

    from epistemic.projectors.exomem_vault import ENTITY_GRAPH_ENDPOINTS, RUNTIME_ENDPOINTS

    agent = _Agent(PET_TWIN, _profile(tmp_path))
    result = _run(tmp_path, agent, case_ids=[PET_TWIN], shapes=("hooked",), repeats=1)

    row = result["cases"][0]["runs"][0]
    assert (row["status"], row["assertions"]) == ("scored", {"twin_left_no_referent": "pass"})
    assert "fresh" not in _run_file(tmp_path, PET_TWIN, "hooked")["sessions"]
    snapshot = json.loads(
        (tmp_path / "out" / PET_TWIN / "hooked" / "attempt-01" / "end-snapshot.json").read_text()
    )
    endpoints = set(snapshot["projector"]["endpoints_used"])
    assert set(ENTITY_GRAPH_ENDPOINTS) | set(RUNTIME_ENDPOINTS) <= endpoints


@pytest.mark.parametrize(
    ("faults", "statuses", "case"),
    [(1, ["blocked", "scored"], "pass"), (2, ["blocked", "blocked"], "incomplete")],
)
def test_a_harness_fault_is_never_scored_and_is_rerun_under_the_same_pins(
    tmp_path, faults: int, statuses: list[str], case: str
) -> None:
    """Fails when a faulted run is scored, is not rerun, or a case without its repeats passes."""

    agent = _Agent(PET_TWIN, _profile(tmp_path), faults=faults)
    result = _run(tmp_path, agent, case_ids=[PET_TWIN], repeats=1, max_attempts=2)

    rows = result["cases"][0]["runs"]
    assert [row["status"] for row in rows] == statuses
    assert result["cases"][0]["status"] == case
    assert rows[0]["assertions"] == {} and "exit code 1" in rows[0]["fault_reason"]

    # Same pins: the argv differs only in its own attempt directory and session id.
    def pinned(argv: list[str], attempt: int) -> list[str]:
        session = argv.index("--session-id") + 1
        return [a.replace(f"attempt-{attempt:02d}", "attempt") for a in argv[:session] + argv[session + 1 :]]

    assert pinned(agent.calls[0], 1) == pinned(agent.calls[1], 2)  # each attempt's first turn


def test_a_report_read_mid_run_or_left_by_an_interrupted_run_says_partial(tmp_path) -> None:
    """Fails when report.json claims a complete run while a requested case or shape has no rows."""

    class Interrupted(Exception):
        pass

    report = tmp_path / "out" / "report.json"
    seen: list[dict] = []

    def before(argv: list[str]) -> None:
        if _plugin(argv) is not None:  # the hooked shape starts after hookless finished
            seen.append(json.loads(report.read_text()))
            raise Interrupted

    agent = _Agent(PET_TWIN, _profile(tmp_path), before=before)
    with pytest.raises(Interrupted):
        _run(tmp_path, agent, case_ids=[PET_TWIN], shapes=("hookless", "hooked"), repeats=1)

    (in_progress,) = seen
    left = json.loads(report.read_text())
    assert [(row["shape"], len(row["runs"])) for row in left["cases"]] == [("hookless", 1)]
    assert (in_progress["partial"], left["partial"]) == (True, True)


@pytest.mark.parametrize(
    ("init_for", "model", "repeats", "calls", "reason", "statuses"),
    [
        (lambda call: {"memory_paths": {"auto": "/elsewhere/projects/x/memory/"}}, MODEL, 1, 1,
         "pinned profile", ["aborted"]),
        (lambda call: {}, "sonnet", 1, 1, "not an alias", ["aborted"]),
        (lambda call: {"claude_code_version": "9.9.9"} if call == 4 else {}, MODEL, 2, 4,
         "'9.9.9', but the run pins '2.1.296'", ["scored", "aborted"]),
    ],
    ids=["wrong_profile", "model_alias", "client_version_drift"],
)
def test_a_deterministic_fault_or_pin_drift_stops_the_whole_driver_run(
    tmp_path, init_for, model: str, repeats: int, calls: int, reason: str, statuses: list[str]
) -> None:
    """Fails when a fault no rerun can clear is rerun, a changed pin or an alias keeps
    scoring, or the report of a stopped run hides that it is partial."""

    agent = _Agent(PET_TWIN, _profile(tmp_path), init_for=init_for)
    with pytest.raises(_driver().DriverAbort) as stopped:
        _run(tmp_path, agent, case_ids=[PET_TWIN], shapes=("hookless", "hooked"), model=model,
             repeats=repeats)

    assert reason in str(stopped.value)
    assert len(agent.calls) == calls
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert reason in manifest["aborted"] and manifest["model"] == model
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["partial"] is True and reason in report["aborted"]
    assert [row["status"] for row in report["cases"][0]["runs"]] == statuses


def test_the_child_cannot_update_itself_take_an_injected_packet_or_bill_a_key(tmp_path) -> None:
    """Fails when the client may auto-update mid-baseline, a hook may inject a packet,
    or an inherited API credential replaces the profile's login."""

    removed = ("EXOMEM_RETRIEVE_INJECT", "KB_RETRIEVE_INJECT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    agent = _Agent(PET_TWIN, _profile(tmp_path))
    parent = {"PATH": "/usr/bin", "HOME": str(tmp_path), **{name: "set" for name in removed}}
    _run(tmp_path, agent, case_ids=[PET_TWIN], repeats=1, parent_env=parent)

    for env in agent.envs:
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["CLAUDE_CONFIG_DIR"] == str(agent.profile)
        assert not set(removed) & set(env)


def test_an_anchor_only_the_recency_prior_supplied_does_not_serve(tmp_path) -> None:
    """Fails when anything reached through an ordinary packet's recency-only anchor serves
    (the anchor, a unit of its page, a pointer to its page), or a named anchor beside it
    stops serving (ruling 1c)."""

    from epistemic.journeys.referent_capture import seed_inputs
    from epistemic.projectors.exomem_vault import VaultProjector

    from exomem.memory_refs import memory_ref

    vault = tmp_path / "vault"
    seed_inputs(vault)
    exomem_id = _write_referent(vault, PET)
    snapshot = VaultProjector(vault).project(phase="hookless", taken_at="2026-10-11T00:00:00Z")
    owner = _key(PET)["owner"]["ref"]
    anchors = [
        {"ref": memory_ref(exomem_id), "path": f"{_entity_id()}.md", "status": "resolved",
         "evidence": ["recency"]},
        {"ref": f"{owner}.md", "path": f"{owner}.md", "status": "resolved",
         "evidence": ["recency", "exact_alias"]},
    ]
    unit = {"ref": memory_ref(exomem_id), "provenance": {"path": f"{_entity_id()}.md", "anchor": memory_ref(exomem_id)}}
    (pointer,) = _unit_pointer(memory_ref(exomem_id), f"{_entity_id()}.md")[0][0]["pointers"]
    packet = {"anchors": anchors, "units": [unit], "pointers": [pointer]}
    rows = _driver().served_refs([{"readable": True, "packet": packet}], snapshot)
    assert {row["section"] for row in rows if row["item_id"] == _entity_id()} == {"anchors", "units", "pointers"}
    assert {(row["item_id"], row["serves"]) for row in rows} == {(_entity_id(), False), (owner, True)}


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
@pytest.mark.parametrize("hot", ["ordinary_note", "entity_page"], ids=["recency_carry", "fifth_clause"])
def test_the_products_real_recency_packets_serve_nothing(tmp_path, monkeypatch, hot: str) -> None:
    """Fails when the product renames its recency carry or recency evidence, or when a
    real recency packet still serves its page, through its anchor, units or pointers.

    Drives the product: one governed edit makes one page hot, then a turn that names
    nothing asks from a fresh session key. A hot ordinary note is carried on recency
    (ruling 1b); a hot entity page is an anchor row, which the resolver's fifth clause
    resolves on recency alone inside an ordinary packet (ruling 1c).
    """

    import os
    import time
    import uuid

    from epistemic.journeys.referent_capture import seed_inputs
    from epistemic.projectors.exomem_vault import VaultProjector

    from exomem import commands, lexstore, writer_lease

    vault = tmp_path / "vault"
    for name, value in {
        "EXOMEM_VAULT_PATH": vault, "EXOMEM_STATE_ROOT": tmp_path / "state",
        "EXOMEM_WRITER_LEASE_STATE_DIR": tmp_path / "leases", "EXOMEM_DISABLE_EMBEDDINGS": "1",
    }.items():
        monkeypatch.setenv(name, str(value))
    seed_inputs(vault)
    if hot == "ordinary_note":
        page = "Knowledge Base/Notes/Research/kiln-schedule-review.md"
        target = sorted((vault / "Knowledge Base/Entities/Organizations").glob("*.md"))[0].stem
        (vault / page).parent.mkdir(parents=True, exist_ok=True)
        (vault / page).write_text(
            "---\ntype: research-note\nstatus: active\nupdated: 2026-09-10\n---\n\n"
            "# Kiln schedule review\n\n## Summary\n\nWhy the kiln schedule moved to nights.\n\n"
            "- [decision] The kiln runs overnight so the day shift can glaze. ^k-decision\n\n"
            f"## Relations\n\n- relates_to [[{target}]]\n",
            encoding="utf-8",
        )
        old_text, new_text = "to nights.", "to nights in spring."
    else:
        page = f"{_key(PET)['owner']['ref']}.md"
        old_text = (vault / page).read_text(encoding="utf-8").strip().splitlines()[-1]
        new_text = old_text + " Still on the team."
    old = time.time() - 30 * 86400
    for path in (vault / "Knowledge Base").rglob("*.md"):
        os.utime(path, (old, old))
    lexstore.ensure_fresh(vault)
    edit = next(command for command in commands.PRODUCT_COMMANDS if command.name == "edit_memory")
    writer_lease.invoke_command(
        edit, vault, path=page, why="make one page hot",
        operation={"kind": "replace_string", "old_string": old_text, "new_string": new_text},
    )

    packet = commands.op_activate_context(vault, turn="continue", session=f"fresh-{uuid.uuid4().hex}")

    carried = packet["generation"].get("carried_by") == _driver().RECENCY_CARRY
    assert carried is (hot == "ordinary_note"), (packet["generation"], packet.get("abstention"))
    (anchor,) = packet["anchors"]
    assert _driver()._recency_only(anchor)
    snapshot = VaultProjector(vault).project(phase="hookless", taken_at="2026-10-11T00:00:00Z")
    rows = _driver().served_refs([{"readable": True, "packet": packet}], snapshot)
    # The fifth-clause packet also serves the page's units; offline, the carry's unit lanes fail.
    reached = {row["section"] for row in rows if row["item_id"] == page.removesuffix(".md")}
    assert {"anchors", *(("units",) if hot == "entity_page" else ())} <= reached
    assert not any(row["serves"] for row in rows)


def test_the_default_client_profile_is_refused(tmp_path) -> None:
    """Fails when a run could spend turns on the default profile."""

    from epistemic.journeys import f27_replay as client

    default = tmp_path / ".claude"
    default.mkdir()
    agent = _Agent(PET_TWIN, default)
    with pytest.raises(client.JourneySetupError, match="default client profile"):
        _run(tmp_path, agent, case_ids=[PET_TWIN], repeats=1)
    assert agent.calls == []


@pytest.mark.parametrize(
    ("check", "shape", "init", "plugin", "extra", "reason"),
    [
        ("abort", "hookless", {"memory_paths": {"auto": "/elsewhere/projects/x/memory/"}}, False, None, "pinned profile"),
        ("abort", "hookless", {"apiKeySource": "ANTHROPIC_API_KEY"}, False, None, "API key"),
        ("abort", "hookless", {}, True, None, "loaded plugins"),
        ("abort", "hooked", {}, False, None, "loaded plugins"),
        ("abort", "hookless", {}, False, "hook", "hookless shape"),
        ("block", "hookless", {"mcp_servers": [{"name": "exomem", "status": "failed"}]}, False, None, "not connected"),
        ("block", "hookless", {}, False, "denial", "denied"),
        ("block", "hooked", {}, True, "packet", "injected retrieved memory"),
    ],
)
def test_a_session_off_the_pinned_account_or_surface_is_a_fault(
    tmp_path, check: str, shape: str, init: dict, plugin: bool, extra: str | None, reason: str
) -> None:
    """Fails when a session on another profile, a key, the wrong shape, a dead server,
    a denial or a hook-injected packet scores, or a setup fault is rerun as a blocked run."""

    from epistemic.journeys import f27_replay as client

    from exomem._hooks.exomem_retrieve_nudge import _bounded_block, _packet_lines

    profile = _profile(tmp_path)
    lines = [_init(profile, plugin=client.PLUGIN_DIR if plugin else None, **init)]
    if extra == "hook":
        lines.append(_hook("UserPromptSubmit", "reminder"))
    if extra == "packet":
        packet = {"units": [{"ref": "exomem://memory/00000000-0000-0000-0000-000000000001", "text": "x"}]}
        lines.append(_hook("UserPromptSubmit", _bounded_block(_packet_lines(packet), 4000)))
    lines.append(_result(denials=[{"tool_name": "Bash"}] if extra == "denial" else ()))
    stdout, arm = _stream(*lines), client.ARMS[shape]
    abort = _driver().surface_mismatch(stdout, arm=arm, plugin_dir=client.PLUGIN_DIR, profile=profile)
    block = _driver().session_fault(stdout)
    fault = abort if check == "abort" else block
    assert fault is not None and reason in fault
    assert (abort is None) is (check == "block")


def test_a_dirty_benchmarks_tree_refuses_the_product_pin(tmp_path) -> None:
    """Fails when an uncommitted change to the driver or assertions runs under a clean-looking SHA."""

    from epistemic.journeys import f27_replay as client

    repo = tmp_path / "repo"
    for part in ("src", "plugins", "benchmarks"):
        (repo / part).mkdir(parents=True)
        (repo / part / "file.txt").write_text("one\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "seed")
    assert _driver().product_identity(repo)["benchmarks_tree"]
    (repo / "benchmarks" / "file.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(client.JourneySetupError, match="benchmarks/"):
        _driver().product_identity(repo)


def test_a_digest_mismatch_refuses_the_whole_run_before_any_turn(tmp_path) -> None:
    """Fails when an edited seed vault is found only per run, after a manifest and attempts.

    The case files are refused by the case-set build too; the seed is read only when
    a run lays its vault, so the up-front check is what refuses the whole run.
    """

    from epistemic.journeys.referent_capture import (
        RECEIPT_PATH,
        SEED_PATH,
        ReferentCaseError,
        reset_cache,
    )

    root = tmp_path / "repo"
    shutil.copytree(ROOT / "benchmarks/epistemic/fixtures/sequence7", root / "benchmarks/epistemic/fixtures/sequence7")
    (root / RECEIPT_PATH).parent.mkdir(parents=True)
    shutil.copy(ROOT / RECEIPT_PATH, root / RECEIPT_PATH)
    edited = root / SEED_PATH
    edited.write_bytes(edited.read_bytes() + b"\n")
    agent = _Agent(PET_TWIN, _profile(tmp_path))
    reset_cache()
    with pytest.raises(ReferentCaseError, match="frozen digest"):
        _run(tmp_path, agent, case_ids=[PET_TWIN], cases_root=root)
    reset_cache()
    assert agent.calls == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("polarity", "outcomes", "status"),
    [
        ("positive", ["pass", "fail", "pass"], "pass"),
        ("positive", ["fail", "fail", "pass"], "fail"),
        ("positive", ["pass", "fail", "judge_pending"], "judge_pending"),
        ("twin", ["pass", "pass", "pass"], "pass"),
        ("twin", ["pass", "fail", "pass"], "fail"),
        ("positive", ["pass", "pass"], "incomplete"),
    ],
)
def test_case_status_follows_the_amendment_rule(polarity, outcomes, status) -> None:
    """Fails when a positive needs 3 of 3, a twin tolerates a false positive, or a short case passes."""

    assert _driver().case_status(polarity, outcomes, 3) == status


@pytest.mark.parametrize("evidence", [["recency"], ["recency", "exact_alias"]])
@pytest.mark.parametrize("lane", ["units", "graph"])
def test_a_neighbour_keeps_its_origin_when_mapping_served_refs(evidence, lane) -> None:
    """A recency anchor must not credit its neighbour through a unit or pointer."""

    from types import SimpleNamespace

    from exomem import working_set

    origin, neighbour = "Entities/Origin.md", "Entities/Neighbour.md"
    snapshot = SimpleNamespace(items=[
        SimpleNamespace(id=path.removesuffix(".md"), locator=path, raw={})
        for path in (origin, neighbour)
    ])
    unit = working_set.LaneItem(
        role="example", level="unit", ref=f"{neighbour}#fact", path=neighbour,
        title="Neighbour", text="A synthetic fact.", lifecycle="active", updated="",
        anchor=origin,
    )
    items = [unit] if lane == "units" else working_set._graph_lane(
        SimpleNamespace(id="example"),
        anchors=[SimpleNamespace(ref=origin, path=origin, neighbourhood=[neighbour])],
    )
    packet = working_set.build_packet(
        items=items, anchors=[{"ref": origin, "path": origin, "evidence": evidence}],
        roles=[{"id": "example", "lane": lane}], current_state=[], ambiguity=[],
        missing=[], max_chars=4000, generation={}, status="resolved",
    )

    rows = _driver().served_refs([{"readable": True, "packet": packet}], snapshot)

    neighbour_rows = [row for row in rows if row["item_id"] == "Entities/Neighbour"]
    assert neighbour_rows
    assert any(row["serves"] for row in neighbour_rows) is ("exact_alias" in evidence)


@pytest.mark.parametrize("turns, expected", [([None], None), ([2, None], None), ([2, 3], 5), ([], 0)])
def test_unknown_invocation_usage_does_not_become_a_complete_turn_total(turns, expected) -> None:
    """A missing terminal result must not understate usage as zero or a complete subtotal."""

    driver = _driver()
    session = {"invocations": []}
    for count in turns:
        stdout = "" if count is None else _stream({**_result(), "num_turns": count})
        driver._record_invocation(session, "synthetic", [], _Proc(stdout), 1.0)
    record = {
        "sessions": {"capture": session}, "attempt": 1, "status": "blocked",
        "fault_reason": "synthetic interruption", "assertions": {},
        "repeat_outcome": None, "run_wall_s": 1.0,
    }

    assert driver._table_row(record)["agent_turns"] == expected


@pytest.mark.parametrize("withheld", ["origin", "target", "missing", None])
@pytest.mark.parametrize("memory_origin", [False, True])
def test_pointer_origin_and_target_remain_governed(tmp_path, withheld, memory_origin) -> None:
    """Preserving a pointer's origin must expose neither a hidden origin nor a hidden target."""

    from test_governance_egress import _external, write_rule, write_scope

    from exomem import working_set
    from exomem.governance import egress
    from exomem.governance.principal import request_scope
    from exomem.memory_refs import memory_ref

    origin, target = "Knowledge Base/origin.md", "Knowledge Base/target.md"
    origin_id = "00000000-0000-0000-0000-000000000001"
    for path in (origin, target):
        page = tmp_path / path
        page.parent.mkdir(parents=True, exist_ok=True)
        metadata = f"exomem_id: {origin_id}\n" if path == origin else ""
        page.write_text(f"---\ntype: note\n{metadata}---\n\nA synthetic page.\n", encoding="utf-8")
    write_scope(tmp_path, paths=f"{withheld or 'unrelated'}.md")
    write_rule(tmp_path, ceiling=0)
    origin_ref = memory_ref(origin_id) if memory_origin else origin
    item = working_set.LaneItem(
        role="example", level="page", ref="missing" if withheld == "missing" else target,
        path=target, title="A pointer",
        text="", lifecycle="active", updated="", anchor=origin_ref,
    )
    packet = working_set.build_packet(
        items=[item], anchors=[], roles=[{"id": "example", "lane": "graph"}],
        current_state=[], ambiguity=[], missing=[], max_chars=4000,
        generation={}, status="resolved",
    )
    with request_scope(_external()):
        guarded = egress.guard_working_set(
            tmp_path, packet, egress.AnnotatedHits(hits=[], withheld_paths=frozenset(), active=True),
        )

    assert guarded is not None
    assert bool(guarded["pointers"]) is (withheld is None)
    if withheld is not None:
        assert f"Knowledge Base/{withheld}.md" not in json.dumps(guarded)
        if withheld == "origin":
            assert origin_ref not in json.dumps(guarded)
