"""Typed adoption jobs retain the merged collection workflow and source identities."""

import pytest
import test_single_purpose_tools_mcp as pilot_mcp
from test_single_purpose_tools_mcp import _call, _session

from exomem import commands, find, operation_entries, request_budget, writer_lease
from exomem.command_surface import resolve_invocation
from exomem.vault import parse_frontmatter

build = pilot_mcp.build


@pytest.mark.parametrize(
    "tool,source,selector,kind,fields",
    [
        ("read_import", "adopt_vault", "mode", "scan-only", {"path": "Legacy"}),
        ("import_memory", "adopt_vault", "mode", "copy-as-sources",
         {"selected_paths": ["Legacy/source.md"]}),
        ("read_import", "adoption_studio", "action", "status", {}),
        ("import_memory", "adoption_studio", "action", "start", {"collection": "Example collection"}),
        ("import_memory", "adoption_studio", "action", "select", {
            "run_id": "run", "include": [{"path": "Legacy", "source_kind": "article", "domain": "science"}],
        }),
        ("import_memory", "adoption_studio", "action", "apply", {
            "run_id": "run", "plan_id": "plan", "continuation": "opaque-cursor", "retry_failed": True,
        }),
        ("import_memory", "adoption_studio", "action", "finish", {
            "run_id": "run", "write_manifest": False,
        }),
    ],
)
def test_adoption_jobs_preserve_exact_collection_arguments(tool, source, selector, kind, fields):
    command, flat = resolve_invocation(tool, {"operation": {"kind": kind, **fields}}, commands.PRODUCT_COMMANDS)
    assert command.name == source
    assert flat == {selector: kind, **fields}
    assert operation_entries.spell_call(source, flat, "nested") == (tool, {"operation": {"kind": kind, **fields}})
    assert operation_entries.unspell_call(tool, {"operation": {"kind": kind, **fields}}, "nested") == (source, flat)


@pytest.mark.parametrize("kind,fields", [("plan", {"run_id": "run"}), ("finish", {"run_id": "run", "write_manifest": False})])
def test_adoption_previews_and_reports_keep_their_persisted_write_effect(kind, fields):
    command, flat = resolve_invocation("import_memory", {"operation": {"kind": kind, **fields}}, commands.PRODUCT_COMMANDS)
    assert commands.invocation_is_read_only(command, flat) is False


def test_mcp_collection_adoption_keeps_rules_continuations_retries_and_recorded_verification(vault, build, monkeypatch):
    originals = {"Legacy/a.md": b"# Alpha\n\nOriginal alpha.\n", "Legacy/b.md": b"# Beta\n\nOriginal beta.\n"}
    for relative, content in originals.items():
        path = vault / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
    find.clear_cache()
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "1")
    request_budget.reset_resolution_cache()

    async def scenario(client):
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert {"read_import", "import_memory"} <= tools.keys()
        assert not {"adopt_vault", "adoption_studio"} & tools.keys()
        branches = {branch["properties"]["kind"]["const"]: branch
                    for branch in tools["import_memory"].input_schema["properties"]["operation"]["oneOf"]}
        include = branches["select"]["properties"]["include"]["items"]["anyOf"]
        assert {item["type"] for item in include} == {"string", "object"}
        assert "collection" in branches["start"]["properties"]
        assert "continuation" in branches["apply"]["properties"]
        assert "report" not in branches

        async def change(kind, **fields):
            result = await _call(client, "import_memory", {
                "operation": {"kind": kind, **fields}, "response_detail": "full",
            })
            if "ok" in result:
                assert result["ok"] is True, result
                diagnostics = result["diagnostics"]
                return diagnostics.get("leaf_result", diagnostics)
            # A cursor replay makes no mutation and retains the source's leaf-only reply.
            assert "run_id" in result, result
            return result

        started = await change("start", path="Legacy", collection="Example archive")
        run_id = started["run_id"]
        fields = {"run_id": run_id, "include": [{"path": "Legacy", "source_kind": "book", "domain": "media"}]}
        first = await _call(client, "import_memory", {"operation": {"kind": "select", **fields}}, key="adoption-select")
        retry = await _call(client, "adoption_studio", {"action": "select", **fields}, key="adoption-select")
        assert first["ok"] is True and retry == first

        planned = await change("plan", run_id=run_id)
        assert planned["continuation"]
        for _ in range(4):
            if not planned.get("continuation"):
                break
            planned = await change("plan", run_id=run_id, continuation=planned["continuation"])
        assert not planned.get("continuation")
        plan_id = planned["plan"]["plan_id"]
        applied = await change("apply", run_id=run_id, plan_id=plan_id)
        cursor = applied["continuation"]
        assert cursor
        applied = await change("apply", run_id=run_id, plan_id=plan_id, continuation=cursor)
        replay = await change("apply", run_id=run_id, plan_id=plan_id, continuation=cursor)
        assert replay["progress"] == applied["progress"]
        assert replay.get("continuation") == applied.get("continuation")
        assert not applied.get("continuation")

        def ensure_writer(*args, **kwargs):
            raise AssertionError("adoption status must not acquire writer authority")

        with monkeypatch.context() as read_patch:
            read_patch.setattr(writer_lease.LeaseManager, "ensure_writer", ensure_writer)
            status = await _call(client, "read_import", {"operation": {"kind": "status", "run_id": run_id}})
        assert status["verified_unchanged"] == status["verified_total"] == len(originals)
        for item in status["outcomes"].values():
            frontmatter, _, _ = parse_frontmatter((vault / item["target_path"]).read_text())
            assert (frontmatter["source_type"], frontmatter["domain"]) == ("book", "media")
        finished = await change("finish", run_id=run_id, write_manifest=False)
        assert finished["verified_unchanged"] == finished["verified_total"] == len(originals)
        assert "import_memory" in finished["finish"]["handoff"]["prompt_text"]
        assert "adoption_studio(" not in finished["finish"]["handoff"]["prompt_text"]

    try:
        _session(build(), scenario)
        assert {relative: (vault / relative).read_bytes() for relative in originals} == originals
    finally:
        request_budget.reset_resolution_cache()
