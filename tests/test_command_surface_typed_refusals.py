"""Domain refusals reach MCP clients as the coded envelope, not a bare ToolError."""

from __future__ import annotations

import asyncio
from pathlib import Path

from exomem import server


def test_edit_refusal_over_mcp_carries_its_code(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    rel = "Knowledge Base/Notes/Research/Taste/scratch-taste.md"
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: research-note\nproject: taste\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [taste]\n---\n# Scratch\n\nbody.\n",
        encoding="utf-8",
    )
    mcp = server.build_server(require_auth=False)

    result = asyncio.run(
        mcp.call_tool(
            "edit_memory",
            {
                "path": rel,
                "why": "fix a typo",
                "operation": {
                    "kind": "replace_string",
                    "old_string": "text that is not on the page",
                    "new_string": "x",
                },
            },
            run_middleware=True,
        )
    )

    error = result.structured_content["error"]
    assert error["code"] == "STRING_NOT_FOUND"
    assert error["message"].endswith("Nothing was saved.")


def test_unknown_filter_field_over_mcp_is_a_refusal_naming_its_path(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server.build_server(require_auth=False)

    result = asyncio.run(
        mcp.call_tool(
            "ask_memory",
            {"query": "anything", "filters": {"scope": "kb"}},
            run_middleware=True,
        )
    )

    error = result.structured_content["result"]["error"]
    assert error["code"] == "INVALID_FILTER_FIELD"
    assert "'scope'" in error["message"]


def _serve(tmp_path: Path, monkeypatch):
    """An initialized vault and a `call(tool, arguments)` that goes through the MCP middleware."""
    from conftest import initialize_vault_state_offline

    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="typed refusal tests")
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    for switch in ("EMBEDDINGS", "MEDIA_EXTRACTION", "CLIP", "FILE_WATCHER"):
        monkeypatch.setenv(f"EXOMEM_DISABLE_{switch}", "1")
    mcp = server.build_server(require_auth=False)

    def call(tool: str, arguments: dict) -> dict:
        content = asyncio.run(mcp.call_tool(tool, arguments, run_middleware=True)).structured_content
        return content["result"] if set(content) == {"result"} else content

    return vault, call


def _merge_retry(arguments: dict, retry_arguments: dict) -> dict:
    """What an agent sends: its call with `retry.arguments` laid over it, key by key and through nested objects."""
    merged = dict(arguments)
    for key, value in retry_arguments.items():
        current = merged.get(key)
        merged[key] = (
            _merge_retry(current, value) if isinstance(value, dict) and isinstance(current, dict) else value
        )
    return merged


def test_a_refused_observe_commits_when_its_retry_is_followed_as_written(
    tmp_path: Path, monkeypatch
) -> None:
    """The retry names the tool and places its arguments where that tool takes them.

    `change_observations` takes one `operation` object and nothing else, so the reviewed-none
    decision belongs inside it; a retry that put it beside `operation` was refused again.
    """
    _, call = _serve(tmp_path, monkeypatch)

    def note(title: str, **extra) -> dict:
        source = call(
            "capture_source",
            {
                "title": f"{title} source",
                "source_kind": "session",
                "content": f"{title} was discussed.",
                "response_detail": "full",
            },
        )
        return call(
            "create_note",
            {
                "title": title,
                "note_type": "insight",
                "content": "## Observations\n\n- [operating constraint] Keep retries bounded #reliability\n",
                "sources": [f"{source['diagnostics']['operational']['path']}#Capture"],
                "response_detail": "full",
                **extra,
            },
        )

    first = note("First")
    note(
        "Second",
        relation_disposition="reviewed_none",
        relation_review_reason="Nothing in the vault fits the second page.",
    )
    operation = {
        "kind": "add", "path": first["id"], "category": "rule", "content": "Opens at nine.",
    }

    refused = call("change_observations", {"operation": operation})["error"]

    assert refused["code"] == "RELATION_DISPOSITION_STALE"
    retry = refused["retry"]
    assert retry["tool"] == "change_observations"
    followed = _merge_retry({"operation": operation}, retry["arguments"])
    followed["operation"]["relation_review_reason"] = "The page stands alone: no other page covers its opening hours."
    result = call(retry["tool"], followed)
    assert "error" not in result, result
    written = tmp_path / "vault" / first["diagnostics"]["operational"]["path"]
    assert "Opens at nine." in written.read_text(encoding="utf-8")


def test_a_refused_record_append_names_its_fields_and_resumes_the_held_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """A refusal that keeps a held candidate says so, names the fields at fault and carries the resume call."""
    from record_fixtures import LEDGER_COLLECTION_PATH, ledger_item, setup_ledger_collection

    vault, call = _serve(tmp_path, monkeypatch)
    setup_ledger_collection(vault)
    operation = {
        "kind": "append",
        "collection": LEDGER_COLLECTION_PATH,
        "item": ledger_item(unlisted_channel="digest"),
        "why": "record a published entry",
    }

    refused = call("change_records", {"operation": operation})["error"]

    assert refused["code"] == "SCHEMA_UNKNOWN_FIELD"
    assert "unlisted_channel" in refused["message"]
    assert "Nothing was saved" not in refused["message"]
    assert "held candidate" in refused["message"]
    retry = refused["retry"]
    assert retry["tool"] == "change_records"
    resumed = call(retry["tool"], _merge_retry({"operation": operation}, retry["arguments"]))
    assert "error" not in resumed, resumed


def test_a_refusal_carries_diagnostics_when_the_commands_own_default_is_full() -> None:
    """A call that omits `response_detail` means the command's default, and `govern_memory` defaults to `full`."""
    from exomem import command_surface

    refusal = {"code": "CLOUD_GATEWAY_REQUIRED", "message": "the gateway answers this tool"}

    by_default = command_surface.agent_refusal_result(
        refusal, None, "govern_memory", {}, (), write=False, default_detail="full"
    )
    by_cell_default = command_surface.agent_refusal_result(
        refusal, None, "record_memory", {}, (), write=True, default_detail="compact"
    )

    assert by_default["diagnostics"]["error"]["code"] == "CLOUD_GATEWAY_REQUIRED"
    assert "diagnostics" not in by_cell_default
