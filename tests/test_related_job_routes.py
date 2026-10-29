"""Related jobs select their original execution boundary before authorization."""

import pytest

from exomem import commands, operation_entries
from exomem.cli_ops import OpError
from exomem.command_surface import resolve_invocation


@pytest.mark.parametrize(
    "tool,operation,command,flat",
    [
        ("bootstrap", {"kind": "writer-status"}, "coordination_status", {}),
        ("bootstrap", {"kind": "settings"}, "configure_memory", {"action": "inspect"}),
        ("read_memory", {"kind": "graph", "max_nodes": 12}, "viewer_map", {"max_nodes": 12}),
        ("prepare_note", {"kind": "compile", "sources": []}, "compile_source", {"sources": []}),
        ("read_media", {"kind": "status", "path": "Evidence/clip.mp4"}, "process_media",
         {"operation": "status", "path": "Evidence/clip.mp4"}),
        ("change_session_access", {"kind": "revoke", "grant_id": "g"}, "govern_memory",
         {"operation": "revoke", "grant_id": "g"}),
    ],
)
def test_related_job_routes_keep_the_original_execution_identity(tool, operation, command, flat):
    selected, actual = resolve_invocation(tool, {"operation": operation}, commands.PRODUCT_COMMANDS)
    assert (selected.name, actual) == (command, flat)
    assert operation_entries.spell_call(command, flat, "nested") == (tool, {"operation": operation})


def test_same_name_flat_compatibility_keeps_its_original_route():
    selected, actual = resolve_invocation("read_media", {"path": "clip.mp4"}, commands.PRODUCT_COMMANDS)
    assert selected.name == "read_media"
    assert actual == {"path": "clip.mp4"}


def test_selected_governance_refusal_keeps_authentication_selectors():
    with pytest.raises(OpError) as raised:
        resolve_invocation("manage_authorization_session", {"operation": {
            "kind": "status", "authorization_session": "s", "documents": {},
        }}, commands.PRODUCT_COMMANDS)
    assert raised.value.command.name == "govern_memory"
    assert raised.value.arguments["operation"] == "session"
    assert raised.value.arguments["session_action"] == "status"
    assert "documents" not in raised.value.arguments


def test_unknown_shared_kind_has_no_invented_execution_target():
    with pytest.raises(OpError) as raised:
        resolve_invocation("read_media", {"operation": {"kind": "unknown"}}, commands.PRODUCT_COMMANDS)
    assert raised.value.code == "INVALID_OPERATION"
    assert getattr(raised.value, "command", None) is None


def test_mixed_same_name_calls_do_not_fall_back_to_legacy():
    with pytest.raises(OpError):
        resolve_invocation("read_media", {"operation": {"kind": "status"}, "path": "clip.mp4"},
                           commands.PRODUCT_COMMANDS)


@pytest.mark.parametrize("scope", [pytest.param({}, id="omitted"), {"scope": None}, {"scope": "session"}])
def test_optional_grant_scope_keeps_exact_cross_spelling_arguments(scope):
    grouped = {"operation": "grant", "token": "t", **scope}
    tool, args = operation_entries.spell_call("govern_memory", grouped, "nested")
    assert tool == "grant_session_access"
    selected, flat = resolve_invocation(tool, args, commands.PRODUCT_COMMANDS)
    assert selected.name == "govern_memory"
    assert flat == grouped
    assert operation_entries.unspell_call(tool, args, "nested") == ("govern_memory", grouped)


def test_query_filter_schema_comes_from_the_existing_query_contract():
    from exomem.query_compat import DEFAULT_OP, OPS, filter_items_schema

    item = filter_items_schema()["items"]
    assert item["required"] == ["column"]
    assert set(item["properties"]["op"]["enum"]) == OPS
    assert item["properties"]["op"]["default"] == DEFAULT_OP
    for name in ("record_memory", "plan_memory", "query_dataset"):
        command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
        parameter = next(p for p in command.params if p.name == "filters")
        assert parameter.schema["anyOf"][0] == filter_items_schema()


def test_non_capture_effects_do_not_satisfy_the_capture_hook():
    from exomem._hooks.exomem_capture_nudge import _successful_kb_write

    for name, kind in (("change_settings", "set"), ("manage_authorization_session", "open"),
                       ("change_media", "retry")):
        assert not _successful_kb_write({"name": f"mcp__exomem__{name}", "input": {"operation": {"kind": kind}}})


def test_remote_repair_discovery_keeps_the_existing_maintenance_admission():
    variants = [variant.action for command in commands.product_commands_for("mcp")
                for rendering in command.renderings if rendering.name == "repair_memory"
                for variant in rendering.variants]
    from exomem.writer_lease import REMOTE_WRITE_MAINTENANCE_MODES

    # Remote discovery publishes exactly the maintenance writes remote admission accepts.
    assert set(variants) == REMOTE_WRITE_MAINTENANCE_MODES


def test_dataset_tier_and_video_exclusion_apply_to_contributors():
    tier_one = commands.product_commands_for("mcp", expose_tier2=False)
    assert not any(c.name == "query_dataset" for c in tier_one)
    cloud = commands.product_commands_for("mcp", cloud=True)
    assert not any(c.name == "read_media" for c in cloud)
    assert any(r.name == "read_media" and "status" in r.kinds for c in cloud for r in c.renderings)
