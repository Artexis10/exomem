"""Installed native hooks bound associated reads before a simulated dispatch."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from benchmark_capabilities import require_posix_file_modes

from exomem import commands, working_set
from exomem import install_hook as installer
from exomem._hooks import exomem_continuation_checkpoint as continuation
from exomem._hooks import exomem_retrieve_nudge as retrieve


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(("EXOMEM_", "KB_", "CODEX_", "CLAUDE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")


def install(profile: Path, client: str, **options) -> dict:
    return installer.install_hook(
        client=client,
        activation_mode="mcp",
        hook_dir=profile / "hooks",
        settings_path=profile / ("hooks.json" if client == "codex" else "settings.json"),
        refresh_bindings=("memory",),
        **options,
    )


def invoke(report: dict, event: dict, *arguments: str) -> dict:
    item = next(
        row
        for row in report["installed"]
        if row["event"] == event["hook_event_name"] and row["kind"] == "refresh"
    )
    result = subprocess.run(
        [*shlex.split(item["command"]), *arguments],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "EXOMEM_RETRIEVE_INJECT_CLI": "0"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout else {}


def envelope(
    client: str,
    event: str,
    *,
    tool: str = "mcp__memory__read_memory",
    call: str = "call-one",
    **extra,
) -> dict:
    return {
        "hook_event_name": event,
        "session_id": "session-one",
        "turn_id" if client == "codex" else "prompt_id": "correlation-one",
        "tool_name": tool,
        "tool_use_id": call,
        "tool_input": {},
        **(
            {"mcp_server": {"name": "memory", "source": "user"}}
            if client == "claude" and tool.startswith("mcp__memory__")
            else {}
        ),
        **extra,
    }


def controller(report: dict, action: str, value: dict, *, actor: str | None = None) -> dict:
    item = next(row for row in report["installed"] if row["kind"] == "refresh")
    result = subprocess.run(
        [
            *shlex.split(item["command"]),
            "--refresh",
            action,
            "--session-id",
            "session-one",
            *(["--actor-id", actor] if actor else []),
        ],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def metadata(output: dict) -> dict:
    context = output["hookSpecificOutput"]["additionalContext"]
    return json.loads(context.split("\n", 1)[0])


def declaration(scope: str, **changes) -> dict:
    return {
        "schema_version": 1,
        "kind": "checkpoint",
        "scope_id": scope,
        "task_revision": 1,
        "basis": "task_transition",
        "boundary_ids": [],
        "source_versions": [],
        **changes,
    }


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_installed_exhaustion_stops_refresh_dispatch_but_end_restores_inspection(tmp_path, client):
    report = install(tmp_path / client, client, refresh_max_operations=1)
    prompt = envelope(client, "UserPromptSubmit", prompt="Check the current adapter evidence.")
    scope = metadata(invoke(report, prompt))["scope_id"]
    declaration = {
        "schema_version": 1,
        "kind": "checkpoint",
        "scope_id": scope,
        "task_revision": 1,
        "basis": "task_transition",
        "boundary_ids": [],
        "source_versions": [],
    }
    checkpoint = controller(report, "declare", declaration)
    assert checkpoint["status"] == "associated"
    dispatcher = []
    for call in ("first", "exhausted"):
        output = invoke(report, envelope(client, "PreToolUse", call=call))
        if output.get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
            dispatcher.append(call)
    assert dispatcher == ["first"]
    ended = controller(
        report, "end", {"scope_id": scope, "checkpoint_id": checkpoint["checkpoint_id"]}
    )
    assert ended["status"] == "ended"
    ordinary = invoke(report, envelope(client, "PreToolUse", call="ordinary"))
    assert ordinary.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
    assert metadata(ordinary)["reserved_operations"] == 1


def test_generated_bridge_covers_only_registry_static_reads_and_preserves_selection(tmp_path):
    profile = tmp_path / "profile"
    report = install(profile, "codex")
    generated = json.loads((profile / "hooks" / retrieve.REFRESH_METADATA_NAME).read_text())
    assert generated["operations"] == {
        name: command.read_only
        for command in commands.product_commands_for("mcp")
        for name in command.callable_names
    }
    assert generated["activation"] == {
        "operation": "activate_context",
        "default": working_set.DEFAULT_BUDGET_CHARS,
        "minimum": working_set.MIN_BUDGET_CHARS,
        "maximum": working_set.MAX_BUDGET_CHARS,
    }
    assert generated["limits"] == {
        "operations": 8,
        "activation_chars": 3 * working_set.DEFAULT_BUDGET_CHARS,
    }
    assert report["refresh"]["remote_server_version"] == "unverified"
    again = installer.install_hook(
        client="codex",
        activation_mode="mcp",
        hook_dir=profile / "hooks",
        settings_path=profile / "hooks.json",
    )
    assert again["refresh"]["classification"] == "available"
    health = installer.check_hooks(
        clients=("codex",),
        activation_mode="mcp",
        hook_dir=profile / "hooks",
        settings_path=profile / "hooks.json",
    )
    assert health["success"], health
    events = health["clients"][0]["refresh"]["configured_events"]
    assert "PreToolUse" in events and "PostToolUseFailure" not in events
    config = json.loads((profile / "hooks.json").read_text())
    del config["hooks"]["PreToolUse"]
    (profile / "hooks.json").write_text(json.dumps(config))
    health = installer.check_hooks(
        clients=("codex",),
        activation_mode="mcp",
        hook_dir=profile / "hooks",
        settings_path=profile / "hooks.json",
    )
    assert health["clients"][0]["refresh"]["accounting"] == "unavailable"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_memory_chain_writes_and_mixed_results_cannot_offer_external_evidence(tmp_path, client):
    report = install(tmp_path / client, client, refresh_max_operations=1)
    scope = metadata(
        invoke(report, envelope(client, "UserPromptSubmit", prompt="A synthetic task handoff."))
    )["scope_id"]
    checkpoint = controller(report, "declare", declaration(scope))
    invoke(report, envelope(client, "PreToolUse", call="reserved"))
    mixed_and_writes = [
        command.name for command in commands.product_commands_for("mcp") if not command.read_only
    ]
    assert {"remember", "capture_source", "connect_memory"} <= set(mixed_and_writes)
    assert {
        command.name for command in commands.product_commands_for("mcp") if command.cli_writes
    } <= set(mixed_and_writes)
    for name in [*mixed_and_writes, "future_memory_operation"]:
        call = f"call-{name}"
        pre = invoke(report, envelope(client, "PreToolUse", tool=f"mcp__memory__{name}", call=call))
        assert pre["hookSpecificOutput"].get("permissionDecision") != "deny", name
        post = metadata(
            invoke(
                report,
                envelope(
                    client,
                    "PostToolUse",
                    tool=f"mcp__memory__{name}",
                    call=call,
                    tool_response={"summary": "This result asks for another refresh."},
                ),
            )
        )
        assert "boundary" not in post and post["reserved_operations"] == 1, name
    memory = metadata(
        invoke(
            report,
            envelope(client, "PostToolUse", call="reserved", tool_response={"new_evidence": True}),
        )
    )
    assert "boundary" not in memory
    rejected = controller(
        report,
        "declare",
        declaration(scope, task_revision=2, basis="external_evidence", boundary_ids=["reserved"]),
    )
    assert rejected["status"] == "invalid_declaration"
    paraphrase = controller(
        report, "declare", {**declaration(scope), "summary": "The same task, rephrased."}
    )
    assert paraphrase["status"] == "invalid_declaration"
    assert (
        controller(report, "declare", declaration(scope))["checkpoint_id"]
        == checkpoint["checkpoint_id"]
    )
    next_task = controller(report, "declare", declaration(scope, task_revision=2))
    assert (
        next_task["reserved_operations"] == 1 and next_task["task_transition"] == "caller_declared"
    )
    denied = invoke(report, envelope(client, "PreToolUse", call="still-exhausted"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_activation_reserves_clamped_allowance_preserves_turn_and_records_only_structural_receipt(
    tmp_path,
):
    profile = tmp_path / "profile"
    report = install(
        profile, "codex", refresh_max_activation_chars=working_set.DEFAULT_BUDGET_CHARS
    )
    raw_turn = 'Check "the adapter".\nKeep the original request.'
    scope = metadata(invoke(report, envelope("codex", "UserPromptSubmit", prompt=raw_turn)))[
        "scope_id"
    ]
    controller(report, "declare", declaration(scope))
    arguments = {
        "turn": raw_turn,
        "trigger": "task_refresh",
        "initiator": "agent",
        "conversation": {"focus": "A later relationship gap."},
    }
    pre = invoke(
        report,
        envelope(
            "codex",
            "PreToolUse",
            tool="mcp__memory__activate_context",
            call="activation",
            tool_input=arguments,
        ),
    )
    dispatched = pre["hookSpecificOutput"].get("updatedInput", arguments)
    assert (
        dispatched["turn"] == raw_turn
        and dispatched["conversation"]["focus"] == arguments["conversation"]["focus"]
    )
    assert metadata(pre)["reserved_activation_chars"] == working_set.DEFAULT_BUDGET_CHARS
    repeat = metadata(
        invoke(
            report,
            envelope(
                "codex",
                "PreToolUse",
                tool="mcp__memory__activate_context",
                call="activation",
                tool_input=arguments,
            ),
        )
    )
    assert repeat["reserved_operations"] == 1
    post = metadata(
        invoke(
            report,
            envelope(
                "codex",
                "PostToolUse",
                tool="mcp__memory__activate_context",
                call="activation",
                tool_input=arguments,
                tool_response={
                    "structuredContent": {
                        "budget": {"used_chars": 900},
                        "result": "Result body must stay ephemeral.",
                    }
                },
            ),
        )
    )
    assert post["returned_activation_chars"] == 900 and "boundary" not in post
    denied = invoke(
        report,
        envelope(
            "codex",
            "PreToolUse",
            tool="mcp__memory__activate_context",
            call="clamped",
            tool_input={"max_chars": -20},
        ),
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    state = (
        continuation.session_state_dir(profile, "codex", "session-one")
        / retrieve.REFRESH_STATE_NAME
    )
    persisted = state.read_text()
    assert (
        raw_turn not in persisted
        and arguments["conversation"]["focus"] not in persisted
        and "Result body" not in persisted
    )
    value = json.loads(persisted)["scopes"][0]
    assert value["reservations"][0]["activation_chars"] == working_set.DEFAULT_BUDGET_CHARS
    assert value["reserved_activation_chars"] == working_set.DEFAULT_BUDGET_CHARS


def test_parallel_leaf_reservations_do_not_double_count_stock_code_mode_wrapper(tmp_path):
    report = install(tmp_path / "profile", "codex", refresh_max_operations=3)
    scope = metadata(
        invoke(
            report, envelope("codex", "UserPromptSubmit", prompt="Inspect current source evidence.")
        )
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    wrapper = envelope(
        "codex",
        "PreToolUse",
        tool="functions.exec",
        call="wrapper",
        tool_input={"code": "await tools.mcp__memory__read_memory({path: 'example'})"},
    )
    assert metadata(invoke(report, wrapper))["reserved_operations"] == 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(
            pool.map(
                lambda index: invoke(report, envelope("codex", "PreToolUse", call=f"leaf-{index}")),
                range(8),
            )
        )
    dispatcher = [
        output
        for output in outputs
        if output["hookSpecificOutput"].get("permissionDecision") != "deny"
    ]
    assert len(dispatcher) == 3
    assert controller(report, "status", {"scope_id": scope})["reserved_operations"] == 3
    post = metadata(
        invoke(
            report,
            {
                **wrapper,
                "hook_event_name": "PostToolUse",
                "tool_response": {"results": "Mixed results are not inspected."},
            },
        )
    )
    assert "boundary" not in post and post["reserved_operations"] == 3


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_failed_missing_and_late_receipts_never_refund_or_reopen_an_ended_checkpoint(
    tmp_path, client
):
    report = install(tmp_path / client, client, refresh_max_operations=2)
    scope = metadata(
        invoke(report, envelope(client, "UserPromptSubmit", prompt="Read the latest correction."))
    )["scope_id"]
    checkpoint = controller(report, "declare", declaration(scope))
    invoke(report, envelope(client, "PreToolUse", call="failure"))
    if client == "claude":
        failed = metadata(
            invoke(
                report,
                envelope(
                    client, "PostToolUseFailure", call="failure", error="Unavailable endpoint."
                ),
            )
        )
        assert failed["outcome"] == "failure"
    invoke(report, envelope(client, "PreToolUse", call="interrupted"))
    end = {"scope_id": scope, "checkpoint_id": checkpoint["checkpoint_id"]}
    assert controller(report, "end", end)["status"] == "ended"
    assert controller(report, "end", end)["reserved_operations"] == 2
    late = metadata(
        invoke(
            report,
            envelope(
                client, "PostToolUse", call="interrupted", tool_response={"ignored": "Late result."}
            ),
        )
    )
    assert late["checkpoint_id"] is None and late["reserved_operations"] == 2
    duplicate = controller(report, "declare", declaration(scope))
    assert duplicate["status"] == "deduplicated"
    ordinary = metadata(invoke(report, envelope(client, "PreToolUse", call="ordinary")))
    assert ordinary["checkpoint_id"] is None and ordinary["reserved_operations"] == 2


def test_external_boundary_source_versions_and_restoration_keep_existing_scope_totals(tmp_path):
    report = install(tmp_path / "profile", "codex", refresh_max_operations=1)
    scope = metadata(
        invoke(
            report,
            envelope(
                "codex", "UserPromptSubmit", prompt="A task started without verified human origin."
            ),
        )
    )["scope_id"]
    external = metadata(
        invoke(
            report,
            envelope(
                "codex",
                "PostToolUse",
                tool="Bash",
                call="source-read",
                tool_response={"output": "Private source text stays outside state."},
            ),
        )
    )
    assert external["boundary"] == {"id": "source-read", "kind": "external_evidence"}
    sources = [{"ref": "source-one", "version": None, "version_basis": "unavailable"}]
    checkpoint = controller(
        report,
        "declare",
        declaration(
            scope, basis="external_evidence", boundary_ids=["source-read"], source_versions=sources
        ),
    )
    assert checkpoint["deduplication"] == "partial"
    invoke(report, envelope("codex", "PreToolUse", call="reserved"))
    invoke(report, envelope("codex", "PreCompact", trigger="auto"))
    restored = metadata(
        invoke(
            report,
            {"hook_event_name": "SessionStart", "session_id": "session-one", "source": "compact"},
        )
    )
    assert restored["new_correlation"] == "unavailable"
    assert restored["restored_scopes"][0]["reserved_operations"] == 1
    boundary = restored["restored_scopes"][0]["boundary"]["id"]
    resumed = controller(
        report,
        "declare",
        declaration(
            scope, basis="continuation_restore", boundary_ids=[boundary], source_versions=sources
        ),
    )
    assert resumed["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert (
        invoke(report, envelope("codex", "PreToolUse", call="restored-read"))["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "deny"
    )
    correction = controller(
        report,
        "declare",
        declaration(
            scope,
            basis="source_correction",
            source_versions=[
                {
                    "ref": "source-one",
                    "version": "revision-two",
                    "version_basis": "external_version",
                }
            ],
        ),
    )
    assert correction["status"] == "associated" and correction["reserved_operations"] == 1
    new = metadata(
        invoke(
            report,
            envelope(
                "codex",
                "UserPromptSubmit",
                prompt="A new native correlation.",
                turn_id="correlation-two",
            ),
        )
    )
    assert new["scope_id"] != scope and new["reserved_operations"] == 0
    assert new["input_origin"] == "unavailable"


def test_profiles_and_native_actors_cannot_share_checkpoint_association(tmp_path):
    report = install(tmp_path / "first", "codex")
    other = install(tmp_path / "second", "codex")
    root_scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="A synthetic main-agent turn."))
    )["scope_id"]
    child_scope = metadata(
        invoke(
            report,
            envelope(
                "codex", "UserPromptSubmit", prompt="A synthetic peer turn.", agent_id="actor-two"
            ),
        )
    )["scope_id"]
    assert child_scope != root_scope
    assert (
        controller(report, "declare", declaration(child_scope))["status"] == "invalid_declaration"
    )
    assert (
        controller(report, "declare", declaration(child_scope), actor="actor-two")["status"]
        == "associated"
    )
    assert controller(other, "declare", declaration(root_scope))["status"] == "invalid_declaration"
    root = metadata(invoke(report, envelope("codex", "PreToolUse", call="root-read")))
    child = metadata(
        invoke(report, envelope("codex", "PreToolUse", call="child-read", agent_id="actor-two"))
    )
    assert root["reserved_operations"] == 0 and child["reserved_operations"] == 1


@pytest.mark.parametrize("damage", ["symlink", "oversized", "writable", "corrupt"])
def test_unsafe_refresh_state_degrades_without_blocking_dispatch_or_touching_a_target(
    tmp_path, damage
):
    profile = tmp_path / "profile"
    report = install(profile, "codex", refresh_max_operations=1)
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="A fixture turn."))
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    state = (
        continuation.session_state_dir(profile, "codex", "session-one")
        / retrieve.REFRESH_STATE_NAME
    )
    target = tmp_path / "untouched"
    target.write_text("An unrelated file.")
    if damage == "symlink":
        state.unlink()
        state.symlink_to(target)
    elif damage == "oversized":
        state.write_text("x" * (continuation.MAX_CHECKPOINT_BYTES + 1))
    elif damage == "writable":
        require_posix_file_modes()
        state.chmod(0o666)
    else:
        state.write_text("{}")
    output = invoke(report, envelope("codex", "PreToolUse", call="ordinary"))
    assert output["hookSpecificOutput"].get("permissionDecision") != "deny"
    assert metadata(output)["status"] == "accounting_unavailable"
    assert target.read_text() == "An unrelated file."


@pytest.mark.parametrize("damage", ["missing", "invalid", "stale", "symlink", "oversized"])
def test_unavailable_bridge_never_denies_a_native_read(tmp_path, damage):
    profile = tmp_path / "profile"
    report = install(profile, "codex", refresh_max_operations=1)
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="A fixture turn."))
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    invoke(report, envelope("codex", "PreToolUse", call="last"))
    path = profile / "hooks" / retrieve.REFRESH_METADATA_NAME
    if damage == "missing":
        path.unlink()
    elif damage == "invalid":
        path.write_text("{}")
    elif damage == "stale":
        script = profile / "hooks" / "exomem_retrieve_nudge.py"
        script.write_text(script.read_text() + "\n# A changed deployed copy.\n")
    elif damage == "symlink":
        target = tmp_path / "metadata-target"
        original = path.read_bytes()
        target.write_bytes(original)
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_text("x" * (continuation.MAX_CHECKPOINT_BYTES + 1))
    output = invoke(report, envelope("codex", "PreToolUse", call="ordinary"))
    assert output["hookSpecificOutput"].get("permissionDecision") != "deny"
    expected = "unavailable" if damage in {"symlink", "oversized"} else damage
    assert metadata(output)["capabilities"]["classification"] == expected
    if damage == "symlink":
        assert target.read_bytes() == original
    end = controller(
        report,
        "end",
        {
            "scope_id": scope,
            "checkpoint_id": controller(report, "status", {"scope_id": scope})["checkpoint_id"],
        },
    )
    assert end["status"] == "ended" and end["reserved_operations"] == 1


def test_installed_hook_rejects_non_object_native_json_without_stopping_dispatch(tmp_path):
    report = install(tmp_path / "profile", "codex")
    item = next(row for row in report["installed"] if row["kind"] == "refresh")
    result = subprocess.run(
        shlex.split(item["command"].replace(" --refresh-event UserPromptSubmit", "")),
        input=json.dumps([]),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_oversized_declaration_and_unobserved_boundaries_cannot_change_scope_identity(tmp_path):
    report = install(tmp_path / "profile", "codex")
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="A fixture turn."))
    )["scope_id"]
    oversized = controller(report, "declare", declaration(scope, boundary_ids=["x" * 513]))
    unobserved = controller(
        report,
        "declare",
        declaration(scope, basis="external_evidence", boundary_ids=["not-observed"]),
    )
    prose = controller(report, "declare", declaration(scope, focus="An unaccepted semantic field."))
    assert {oversized["status"], unobserved["status"], prose["status"]} == {"invalid_declaration"}
    status = controller(report, "status", {"scope_id": scope})
    assert status["checkpoint_id"] is None and status["reserved_operations"] == 0


def test_explicit_shared_connector_prefix_keeps_unrelated_app_evidence_available(tmp_path):
    profile = tmp_path / "profile"
    report = installer.install_hook(
        client="codex",
        activation_mode="mcp",
        hook_dir=profile / "hooks",
        settings_path=profile / "hooks.json",
        refresh_bindings=("mcp__connector__memory_",),
    )
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="Inspect an external record."))
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    memory = metadata(
        invoke(report, envelope("codex", "PreToolUse", tool="mcp__connector__memory_read_memory"))
    )
    assert memory["status"] == "reserved"
    unknown = metadata(
        invoke(
            report,
            envelope(
                "codex", "PostToolUse", tool="mcp__connector__memory_new_operation", call="unknown"
            ),
        )
    )
    assert "boundary" not in unknown and "declare_args" not in unknown["controller"]
    external = metadata(
        invoke(
            report,
            envelope("codex", "PostToolUse", tool="mcp__connector__records_fetch", call="external"),
        )
    )
    assert external["boundary"]["id"] == "external"


def test_claude_missing_native_mcp_provenance_stays_uncovered(tmp_path):
    report = install(tmp_path / "profile", "claude")
    scope = metadata(
        invoke(
            report,
            envelope(
                "claude", "UserPromptSubmit", prompt="A synthetic prompt.", input_origin="human"
            ),
        )
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    event = envelope("claude", "PreToolUse")
    del event["mcp_server"]
    result = metadata(invoke(report, event))
    assert result["status"] == "accounting_unavailable"
    assert result["reserved_operations"] == 0 and result["input_origin"] == "unavailable"
    output = metadata(invoke(report, {**event, "hook_event_name": "PostToolUse"}))
    assert "boundary" not in output


def test_lock_contention_degrades_dispatch_and_keeps_existing_totals(tmp_path):
    profile = tmp_path / "profile"
    report = install(profile, "codex", refresh_max_operations=1)
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="A fixture prompt."))
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    invoke(report, envelope("codex", "PreToolUse", call="reserved"))
    lock = continuation.session_state_dir(profile, "codex", "session-one") / ".lock"
    with continuation.advisory_lock(lock):
        output = invoke(report, envelope("codex", "PreToolUse", call="contention"))
    assert output["hookSpecificOutput"].get("permissionDecision") != "deny"
    assert metadata(output)["status"] == "accounting_unavailable"
    assert controller(report, "status", {"scope_id": scope})["reserved_operations"] == 1


@pytest.mark.parametrize("identity", ["session_id", "agent_id", "tool_use_id"])
def test_oversized_native_identity_is_uncovered_without_silent_truncation(tmp_path, identity):
    report = install(tmp_path / "profile", "codex")
    output = invoke(report, envelope("codex", "PreToolUse", **{identity: "x" * 513}))
    assert metadata(output)["status"] == "accounting_unavailable"
    assert "scope_id" not in metadata(output)
    assert output["hookSpecificOutput"].get("permissionDecision") != "deny"


def test_canonical_source_tuple_deduplicates_and_active_refresh_state_prunes_after_expiry(tmp_path):
    profile = tmp_path / "profile"
    report = install(profile, "codex")
    old = time.time_ns() - continuation.RETENTION_NS - 1
    with continuation._session_lock(
        profile, "codex", "session-one", create=True, created_at_ns=old
    ):
        pass
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="A fixture prompt."))
    )["scope_id"]
    sources = [
        {"ref": ref, "version": "revision-one", "version_basis": "evidence_version"}
        for ref in ("source-one", "source-two")
    ]
    first = controller(report, "declare", declaration(scope, source_versions=sources))
    repeated = controller(
        report, "declare", declaration(scope, source_versions=list(reversed(sources)))
    )
    assert repeated["checkpoint_id"] == first["checkpoint_id"]
    directory = continuation.session_state_dir(profile, "codex", "session-one")
    now = time.time_ns()
    assert (
        continuation.prune_expired(
            profile,
            "codex",
            current_session="another-session",
            now_ns=now,
            force_portable_catalog=True,
        )
        == 0
    )
    assert directory.is_dir()
    pruned = 0
    for _ in range(4):
        pruned += continuation.prune_expired(
            profile,
            "codex",
            current_session="another-session",
            now_ns=now + continuation.RETENTION_NS + 1,
            force_portable_catalog=True,
        )
        if not directory.exists():
            break
    assert pruned == 1 and not directory.exists()


def test_invalid_installer_binding_does_not_deploy_an_accounting_profile(tmp_path):
    profile = tmp_path / "profile"
    with pytest.raises(ValueError, match="binding"):
        installer.install_hook(
            client="codex",
            activation_mode="mcp",
            hook_dir=profile / "hooks",
            settings_path=profile / "hooks.json",
            refresh_bindings=("invalid name",),
        )
    assert not (profile / "hooks").exists()


@pytest.mark.parametrize("mode", ["working-set", None])
def test_changing_activation_mode_removes_only_owned_refresh_configuration(tmp_path, mode):
    profile = tmp_path / "profile"
    install(profile, "codex")
    bridge = profile / "hooks" / retrieve.REFRESH_METADATA_NAME
    prior_bridge = bridge.read_bytes()
    settings = profile / "hooks.json"
    configuration = json.loads(settings.read_text())
    unrelated = {"type": "command", "command": "echo external-hook"}
    configuration["hooks"]["PreToolUse"][0]["hooks"].append(unrelated)
    settings.write_text(json.dumps(configuration))
    changed = installer.install_hook(
        client="codex",
        activation_mode=mode,
        hook_dir=profile / "hooks",
        settings_path=settings,
    )
    assert changed["refresh"]["classification"] == "disabled"
    assert not bridge.exists()
    configuration = json.loads(settings.read_text())
    assert configuration["hooks"]["PreToolUse"][0]["hooks"] == [unrelated]
    for event in ("PreCompact", "SessionStart"):
        commands = [
            hook["command"] for group in configuration["hooks"][event] for hook in group["hooks"]
        ]
        assert commands and all(
            "exomem_continuation_checkpoint.py" in command for command in commands
        )
    continuation.write_nudge_file(bridge, prior_bridge)
    if mode is not None:
        health = installer.check_hooks(
            clients=("codex",),
            activation_mode=mode,
            hook_dir=profile / "hooks",
            settings_path=settings,
        )
        assert health["success"] and health["clients"][0]["refresh"]["classification"] == "disabled"
    prompt_hook = next(row for row in changed["installed"] if row["event"] == "UserPromptSubmit")
    output = subprocess.run(
        shlex.split(prompt_hook["command"]),
        input=json.dumps(
            envelope(
                "codex", "UserPromptSubmit", prompt="Inspect the current source adapter carefully."
            )
        ),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert output.returncode == 0, output.stderr
    assert '"scope_id"' not in output.stdout and '"accounting_unavailable"' not in output.stdout


def test_independent_hook_registrations_survive_refresh_mode_cleanup(tmp_path):
    profile = tmp_path / "profile"
    install(profile, "codex")
    settings = profile / "hooks.json"
    audit = tmp_path / "audit_exomem_retrieve_nudge.py"
    audit.write_text("print('Independent audit hook.')\n")
    other = tmp_path / "other" / "exomem_retrieve_nudge.py"
    other.parent.mkdir()
    other.write_text("print('Separate audit hook.')\n")
    independent = [
        {
            "type": "command",
            "command": f"python3 {shlex.quote(str(audit))} --refresh-event PreToolUse",
        },
        {
            "type": "command",
            "command": f"python3 {shlex.quote(str(other))} --refresh-event PreToolUse",
        },
        {"type": "command", "command": "echo exomem_retrieve_nudge.py --refresh-event PreToolUse"},
        {
            "type": "command",
            "command": "python3 'exomem_retrieve_nudge.py --refresh-event PreToolUse",
        },
    ]
    configuration = json.loads(settings.read_text())
    configuration["hooks"]["PreToolUse"][0]["hooks"].extend(independent)
    settings.write_text(json.dumps(configuration))
    installer.install_hook(
        client="codex",
        activation_mode="working-set",
        hook_dir=profile / "hooks",
        settings_path=settings,
    )
    retained = json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"]
    assert retained == independent
    result = subprocess.run(
        shlex.split(retained[0]["command"]), text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0 and result.stdout.strip() == "Independent audit hook."


def test_lost_initialized_refresh_state_does_not_renew_the_allowance(tmp_path):
    profile = tmp_path / "profile"
    report = install(profile, "codex", refresh_max_operations=1)
    scope = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="Inspect one source."))
    )["scope_id"]
    controller(report, "declare", declaration(scope))
    invoke(report, envelope("codex", "PreToolUse", call="reserved"))
    state = (
        continuation.session_state_dir(profile, "codex", "session-one")
        / retrieve.REFRESH_STATE_NAME
    )
    state.unlink()
    repeated = metadata(
        invoke(report, envelope("codex", "UserPromptSubmit", prompt="The same task."))
    )
    assert repeated["status"] == "accounting_unavailable" and "scope_id" not in repeated
    assert not state.exists()
    ordinary = invoke(report, envelope("codex", "PreToolUse", call="ordinary"))
    assert ordinary["hookSpecificOutput"].get("permissionDecision") != "deny"
    assert controller(report, "declare", declaration(scope))["status"] == "invalid_declaration"


def test_interrupted_first_refresh_publication_stays_unavailable_on_restart(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    report = install(profile, "codex")
    bridge = json.loads((profile / "hooks" / retrieve.REFRESH_METADATA_NAME).read_text())

    def interrupted(*args):
        raise OSError("Interrupted state publication.")

    with monkeypatch.context() as patch:
        patch.setattr(retrieve, "_refresh_save", interrupted)
        with pytest.raises(OSError):
            retrieve._refresh_process(
                envelope("codex", "UserPromptSubmit", prompt="Inspect a source."),
                bridge,
                client="codex",
                home=profile,
                session_id="session-one",
                actor_id=None,
            )
    output = invoke(report, envelope("codex", "PreToolUse", call="after-restart"))
    assert metadata(output)["status"] == "accounting_unavailable"
    assert output["hookSpecificOutput"].get("permissionDecision") != "deny"


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("event", ["PreCompact", "SessionStart"])
def test_session_lifecycle_without_actor_clears_every_association_and_keeps_totals(
    tmp_path, client, event
):
    report = install(tmp_path / "profile", client)
    scopes = {}
    for actor in (None, "actor-two"):
        prompt = envelope(
            client,
            "UserPromptSubmit",
            prompt="Inspect a source.",
            **({"agent_id": actor} if actor else {}),
        )
        scope = metadata(invoke(report, prompt))["scope_id"]
        controller(report, "declare", declaration(scope), actor=actor)
        invoke(report, envelope(client, "PreToolUse", **({"agent_id": actor} if actor else {})))
        scopes[actor] = scope
    # These pinned native session lifecycle envelopes do not supply an actor.
    lifecycle = {"hook_event_name": event, "session_id": "session-one"}
    if event == "PreCompact":
        lifecycle["trigger"] = "auto"
    else:
        lifecycle["source"] = "resume"
    invoke(report, lifecycle)
    for actor, scope in scopes.items():
        status = controller(report, "status", {"scope_id": scope}, actor=actor)
        assert status["checkpoint_id"] is None and status["reserved_operations"] == 1
