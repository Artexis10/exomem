"""The f33 driver: two sessions per run, the fresh session's packet, faults and reruns.

Every stream here is FABRICATED, shaped after streams recorded from
``claude 2.1.296 -p --output-format stream-json --verbose --include-hook-events``
on 2026-10-11: the init line's ``memory_paths``, ``apiKeySource``,
``mcp_servers`` and ``plugins``, an ``activate_context`` call answered by a
JSON-text ``tool_result``, a ``UserPromptSubmit`` ``hook_response`` whose
``output`` carries ``hookSpecificOutput.additionalContext``, and the result
line's ``permission_denials``. No test pins stream bytes, and names and values
come from the digest-verified answer key, never restated here.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PET = "f33-pet-owner-v1"
PET_TWIN = "f33-pet-owner-twin-v1"
VERSION = "2.1.296"


def _driver():
    from epistemic.journeys import f33_referent_replay

    return f33_referent_replay


def _key(case_id: str) -> dict:
    from epistemic.journeys.referent_capture import case_key

    return case_key(case_id)


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
        "model": "claude-sonnet-5-5",
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


class _Agent:
    """An offline client: capture turns succeed, and a positive's fresh session activates.

    ``capture`` writes into the run's own vault, found through the environment the
    driver hands the child. ``faults`` makes the first N invocations exit non-zero.
    ``recent_only`` names the referent only in the packet's ``recent_context``.
    """

    def __init__(
        self, case_id: str, profile: Path, *, capture: bool = False, faults: int = 0,
        recent_only: bool = False,
    ):
        self.case_id, self.profile, self.capture, self.faults = case_id, profile, capture, faults
        self.recent_only = recent_only
        self.calls: list[list[str]] = []
        self.exomem_id: str | None = None

    def __call__(self, argv, cwd, env):
        self.calls.append(list(argv))
        init = _init(self.profile, plugin=_plugin(argv))
        if len(self.calls) <= self.faults:
            return _Proc(_stream(init, _result("boom", subtype="error_during_execution")), 1)
        key = _key(self.case_id)
        if _prompt(argv) != _case(self.case_id).question:
            if self.capture and self.exomem_id is None:
                self.exomem_id = _write_referent(Path(env["EXOMEM_VAULT_PATH"]), self.case_id)
            return _Proc(_stream(init, _result()))
        from exomem.memory_refs import memory_ref

        entity = f"Knowledge Base/Entities/Companions/{key['referent']['name']}.md"
        packet = {
            "anchors": [{"ref": memory_ref(self.exomem_id), "path": entity, "status": "resolved"}],
            "units": [{"ref": memory_ref(self.exomem_id), "provenance": {"path": entity}}],
            "pointers": [],
        }
        if self.recent_only:
            served = {"ref": memory_ref(self.exomem_id), "path": entity, "why": "edited"}
            packet = {"recent_context": [served], "anchors": [], "units": [], "pointers": []}
        return _Proc(
            _stream(
                init,
                _call("toolu_1", "mcp__exomem__activate_context", {"turn": _prompt(argv)}),
                _reply("toolu_1", json.dumps(packet)),
                _result(f"The answer is {key['answer_value']}."),
            )
        )


def _case(case_id: str):
    from epistemic.journeys.referent_capture import build_case_set

    return next(case for case in build_case_set() if case.case_id == case_id)


def _run(tmp_path: Path, agent: _Agent, *, case_ids, shapes=("hookless",), **kwargs):
    from epistemic.journeys import f27_replay as client

    return _driver().run_driver(
        out_dir=tmp_path / "out",
        taken_at="2026-10-11T00:00:00Z",
        profile=agent.profile,
        case_ids=case_ids,
        shapes=shapes,
        envelope=client.AgentEnvelope(executable=Path("/opt/agent/claude"), version=f"{VERSION} (x)"),
        product={"head": "0" * 40},
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        runner=agent,
        prominence_writer=lambda _arm, _env: "set",
        **kwargs,
    )


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    return profile


def test_the_fresh_session_packet_is_recorded_and_its_refs_map_to_snapshot_ids(tmp_path) -> None:
    """Fails when the fresh session, its packet or the ref mapping is lost from the run file."""

    agent = _Agent(PET, _profile(tmp_path), capture=True)
    result = _run(tmp_path, agent, case_ids=[PET], repeats=1)

    assert result["manifest"]["claim_status"].startswith("pending acknowledgment")
    row = result["cases"][0]["runs"][0]
    assert (row["status"], row["repeat_outcome"]) == ("scored", "pass")
    run = json.loads((tmp_path / "out" / PET / "hookless" / "attempt-01" / "run.json").read_text())
    capture, fresh = run["sessions"]["capture"], run["sessions"]["fresh"]
    assert [call["label"] for call in capture["invocations"]] == [t for t, _ in _case(PET).turns]
    assert all(call["stream"] for call in (*capture["invocations"], *fresh["invocations"]))
    assert capture["session_id"] != fresh["session_id"]
    assert "--resume" not in fresh["invocations"][0]["argv"]
    assert run["packet"]["captures"][0]["source"] == "activate_context"
    entity_id = f"Knowledge Base/Entities/Companions/{_key(PET)['referent']['name']}"
    assert {ref["item_id"] for ref in run["packet"]["refs"]} == {entity_id}
    assert {name: row["outcome"] for name, row in run["assertions"].items()} == {
        "referent_entity_typed": "pass",
        "referent_key_edge_present": "pass",
        "referent_used_in_fresh_session": "pass",
    }
    assert [call["name"] for call in run["tool_calls"]] == ["mcp__exomem__activate_context"]


def test_a_referent_named_only_in_recent_context_is_not_served(tmp_path) -> None:
    """Fails when recent_context alone counts as serving the referent (ruling of 2026-10-11).

    A capture session edits the referent's page, so recent work names it whatever
    the question was; the answer here states the key value, and "uses" must still fail.
    """

    agent = _Agent(PET, _profile(tmp_path), capture=True, recent_only=True)
    result = _run(tmp_path, agent, case_ids=[PET], repeats=1)

    assert result["manifest"]["uses_rule"]["fixed_before_any_baseline"] is True
    run = json.loads((tmp_path / "out" / PET / "hookless" / "attempt-01" / "run.json").read_text())
    entity_id = f"Knowledge Base/Entities/Companions/{_key(PET)['referent']['name']}"
    assert {(ref["item_id"], ref["serves"]) for ref in run["packet"]["refs"]} == {(entity_id, False)}
    uses = run["assertions"]["referent_used_in_fresh_session"]
    assert (uses["outcome"], "does not serve" in uses["evidence"]) == ("fail", True)


def test_a_quiet_twin_is_scored_on_the_runtime_projection_rather_than_blocked(tmp_path) -> None:
    """Fails when a twin's notice surfaces go unprojected, which blocks every twin run."""

    from epistemic.projectors.exomem_vault import ENTITY_GRAPH_ENDPOINTS, RUNTIME_ENDPOINTS

    agent = _Agent(PET_TWIN, _profile(tmp_path))
    result = _run(tmp_path, agent, case_ids=[PET_TWIN], shapes=("hooked",), repeats=1)

    row = result["cases"][0]["runs"][0]
    assert (row["status"], row["assertions"]) == ("scored", {"twin_left_no_referent": "pass"})
    assert "fresh" not in json.loads(
        (tmp_path / "out" / PET_TWIN / "hooked" / "attempt-01" / "run.json").read_text()
    )["sessions"]
    snapshot = json.loads(
        (tmp_path / "out" / PET_TWIN / "hooked" / "attempt-01" / "end-snapshot.json").read_text()
    )
    endpoints = set(snapshot["projector"]["endpoints_used"])
    assert set(ENTITY_GRAPH_ENDPOINTS) | set(RUNTIME_ENDPOINTS) <= endpoints


@pytest.mark.parametrize(("faults", "statuses", "case"), [(1, ["blocked", "scored"], "pass"), (2, ["blocked", "blocked"], "incomplete")])
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
    ("shape", "init", "plugin", "extra", "reason"),
    [
        ("hookless", {"memory_paths": {"auto": "/elsewhere/projects/x/memory/"}}, False, None, "pinned profile"),
        ("hookless", {"apiKeySource": "ANTHROPIC_API_KEY"}, False, None, "API key"),
        ("hookless", {"mcp_servers": [{"name": "exomem", "status": "failed"}]}, False, None, "not connected"),
        ("hookless", {}, True, None, "loaded plugins"),
        ("hooked", {}, False, None, "loaded plugins"),
        ("hookless", {}, False, "hook", "hookless shape"),
        ("hookless", {}, False, "denial", "denied"),
    ],
)
def test_a_session_off_the_pinned_account_or_surface_is_a_fault(
    tmp_path, shape: str, init: dict, plugin: bool, extra: str | None, reason: str
) -> None:
    """Fails when a session on another profile, a key, a dead server or the wrong shape scores."""

    from epistemic.journeys import f27_replay as client

    profile = _profile(tmp_path)
    lines = [_init(profile, plugin=client.PLUGIN_DIR if plugin else None, **init)]
    if extra == "hook":
        lines.append(_hook("UserPromptSubmit", "reminder"))
    lines.append(_result(denials=[{"tool_name": "Bash"}] if extra == "denial" else ()))
    fault = _driver().session_fault(
        _stream(*lines), arm=client.ARMS[shape], plugin_dir=client.PLUGIN_DIR, profile=profile
    )
    assert fault is not None and reason in fault
    clean = _stream(_init(profile, plugin=client.PLUGIN_DIR if shape == "hooked" else None), _result())
    assert _driver().session_fault(
        clean, arm=client.ARMS[shape], plugin_dir=client.PLUGIN_DIR, profile=profile
    ) is None


@pytest.mark.parametrize("activation", ["none", "errored"])
def test_no_activation_serves_nothing_and_an_unreadable_packet_blocks(tmp_path, activation) -> None:
    """Fails when a session that never activated blocks, or an errored activation is scored."""

    blocks = activation == "errored"
    lines = [_init(tmp_path)]
    if blocks:
        lines += [
            _call("toolu_9", "mcp__exomem__activate_context", {"turn": "q"}),
            # Parseable on purpose: an error envelope is JSON too, and only is_error tells.
            _reply("toolu_9", json.dumps({"error": "lane failed"}), is_error=True),
        ]
    captures, missing = _driver().packet_captures(_stream(*lines, _result()))
    assert captures == []
    assert (missing is not None) is blocks


def test_a_packet_the_hook_delivers_serves_its_refs(tmp_path) -> None:
    """Fails when a hooked session that got its packet from the hook reads as unserved."""

    from epistemic.journeys.referent_capture import seed_inputs
    from epistemic.projectors.exomem_vault import VaultProjector

    from exomem._hooks.exomem_retrieve_nudge import _bounded_block, _packet_lines
    from exomem.memory_refs import memory_ref

    seed_inputs(tmp_path / "vault")
    snapshot = VaultProjector(tmp_path / "vault").project(phase="hooked", taken_at="2026-10-11T00:00:00Z")
    owner = _key(PET)["owner"]["ref"]
    ref = memory_ref(snapshot.item(owner).raw["exomem_id"])
    rendered = _bounded_block(
        _packet_lines({"units": [{"ref": ref, "text": "a seeded fact"}]}), 4000
    )
    captures, missing = _driver().packet_captures(
        _stream(_init(tmp_path), _hook("UserPromptSubmit", rendered), _result())
    )
    assert missing is None
    assert {row["item_id"] for row in _driver().served_refs(captures, snapshot)} == {owner}


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
