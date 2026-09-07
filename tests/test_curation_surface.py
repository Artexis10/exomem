from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import pytest

from exomem import commands
from exomem.commands import product_commands_for
from exomem.writer_lease import LeaseConfig, LeaseManager


def _command():  # noqa: ANN202
    return next(
        command for command in product_commands_for("mcp") if command.name == "maintain_memory"
    )


def test_hosted_v5_contribution_separates_proposal_and_execution_authority() -> None:
    fixture = Path("tests/fixtures/hosted_v5_contributions/governed_curation.json")
    value = json.loads(fixture.read_text(encoding="utf-8"))

    assert value["action_authority"] == {
        "work-item": "structural_suggestions",
        "propose": "structural_suggestions",
        "preview": "structural_suggestions",
        "status": "structural_suggestions",
        "propose-compensation": "structural_suggestions",
        "apply": "restructure_execution",
        "resume": "restructure_execution",
        "apply-compensation": "restructure_execution",
    }
    assert value["confirmation_required_actions"] == ["apply", "apply-compensation"]
    assert value["approved_plan_resume_actions"] == ["resume"]

    cases = {case["id"]: case for case in value["cases"]}
    assert cases["agent-authored-plan-proposal"]["classification"] == (
        "structural_suggestions"
    )
    assert cases["compensation-plan-proposal"]["classification"] == (
        "structural_suggestions"
    )
    assert cases["approved-plan-resume"]["admission"] == "approved_plan_only"
    run_shape = re.compile(r"^cur-[0-9]{8}-[0-9a-f]{12}$")
    assert all(
        run_shape.fullmatch(case["arguments"]["run_id"])
        for case in value["cases"]
        if "run_id" in case["arguments"]
    )


def test_registry_exposes_closed_curation_arguments_and_actions() -> None:
    commands_by_surface = {
        surface: next(
            command
            for command in product_commands_for(surface)
            if command.name == "maintain_memory"
        )
        for surface in ("mcp", "rest", "cli")
    }
    params = {param.name: param for param in commands_by_surface["mcp"].params}

    assert {
        "curation_action",
        "run_id",
        "plan",
        "refs",
        "paths",
        "review_ref",
        "hydration_recheck",
        "expected_plan_fingerprint",
    } <= params.keys()
    assert params["curation_action"].choices == (
        "work-item",
        "propose",
        "preview",
        "status",
        "apply",
        "resume",
        "propose-compensation",
        "apply-compensation",
    )
    assert all(
        command.params == commands_by_surface["mcp"].params
        for command in commands_by_surface.values()
    )


def test_generated_mcp_rest_openapi_and_cli_expose_the_same_curation_selector(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    from exomem import __main__ as cli_main
    from exomem import server

    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "lease-state"))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "curation-test-key")
    mcp = server.build_server(require_auth=False)

    tool = next(tool for tool in asyncio.run(mcp.list_tools()) if tool.name == "maintain_memory")
    mcp_property = tool.to_mcp_tool().model_dump(mode="json")["inputSchema"]["properties"][
        "curation_action"
    ]
    openapi_property = (
        TestClient(mcp.http_app())
        .get("/api/openapi.json", headers={"Authorization": "Bearer curation-test-key"})
        .json()["paths"]["/api/maintain_memory"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["curation_action"]
    )

    parser = argparse.ArgumentParser()
    cli_main._add_command_args(
        parser,
        next(
            command for command in product_commands_for("cli") if command.name == "maintain_memory"
        ),
    )
    parsed = parser.parse_args(
        ["--mode", "curation", "--curation-action", "status", "--run-id", "run"]
    )

    expected = [
        "work-item",
        "propose",
        "preview",
        "status",
        "apply",
        "resume",
        "propose-compensation",
        "apply-compensation",
    ]

    def enum_values(schema: dict[str, object]) -> list[str]:
        if isinstance(schema.get("enum"), list):
            return schema["enum"]  # type: ignore[return-value]
        return next(
            item["enum"]
            for item in schema.get("anyOf", [])  # type: ignore[union-attr]
            if isinstance(item, dict) and isinstance(item.get("enum"), list)
        )

    assert enum_values(mcp_property) == expected
    assert enum_values(openapi_property) == expected
    assert parsed.curation_action == "status"


@pytest.mark.parametrize("action", ["work-item", "preview", "status"])
def test_curation_read_actions_are_classified_read_only(action: str) -> None:
    assert commands.invocation_is_read_only(
        _command(), {"mode": "curation", "curation_action": action}
    )


@pytest.mark.parametrize(
    "action",
    ["propose", "apply", "resume", "propose-compensation", "apply-compensation"],
)
def test_curation_write_actions_are_classified_mutating(action: str) -> None:
    assert not commands.invocation_is_read_only(
        _command(), {"mode": "curation", "curation_action": action}
    )


def test_omitted_or_unknown_curation_action_is_conservatively_mutating() -> None:
    command = _command()
    assert not commands.invocation_is_read_only(command, {"mode": "curation"})
    assert not commands.invocation_is_read_only(
        command, {"mode": "curation", "curation_action": "future-action"}
    )


def test_shared_leaf_routes_every_curation_action(vault: Path, monkeypatch) -> None:  # noqa: ANN001
    from exomem import curation

    calls: list[tuple[str, dict[str, object]]] = []

    def recording(name: str):  # noqa: ANN202
        def invoke(_vault: Path, *args, **kwargs):  # noqa: ANN002, ANN202
            if args:
                kwargs["plan"] = args[0]
            calls.append((name, kwargs))
            return {"action": name, "mutated": name not in curation.READ_ONLY_ACTIONS}

        return invoke

    for name in (
        "work_item",
        "propose",
        "preview",
        "status",
        "apply",
        "resume",
        "propose_compensation",
        "apply_compensation",
    ):
        monkeypatch.setattr(curation, name, recording(name.replace("_", "-")))

    plan = {"version": 1, "title": "x", "steps": []}
    commands.op_maintain_memory(
        vault,
        mode="curation",
        curation_action="work-item",
        review_ref="exomem://review/aaaaaaaaaaaaaaaaaaaaaaaa",
        hydration_recheck=3,
    )
    commands.op_maintain_memory(vault, mode="curation", curation_action="propose", plan=plan)
    commands.op_maintain_memory(vault, mode="curation", curation_action="preview", run_id="r")
    commands.op_maintain_memory(vault, mode="curation", curation_action="status", run_id="r")
    commands.op_maintain_memory(
        vault,
        mode="curation",
        curation_action="apply",
        run_id="r",
        plan_id="p",
        expected_plan_fingerprint="f",
        why="because",
    )
    commands.op_maintain_memory(
        vault, mode="curation", curation_action="resume", run_id="r", plan_id="p"
    )
    commands.op_maintain_memory(
        vault, mode="curation", curation_action="propose-compensation", run_id="r"
    )
    commands.op_maintain_memory(
        vault,
        mode="curation",
        curation_action="apply-compensation",
        run_id="r",
        plan_id="p",
        expected_plan_fingerprint="f",
        why="because",
    )

    assert [name for name, _kwargs in calls] == [
        "work-item",
        "propose",
        "preview",
        "status",
        "apply",
        "resume",
        "propose-compensation",
        "apply-compensation",
    ]
    assert calls[0][1] == {
        "refs": None,
        "paths": None,
        "review_ref": "exomem://review/aaaaaaaaaaaaaaaaaaaaaaaa",
        "hydration_recheck": 3,
    }


def test_unknown_action_and_foreign_mode_arguments_fail_before_curation_dispatch(
    vault: Path, monkeypatch
) -> None:  # noqa: ANN001
    from exomem import curation

    called = False

    def forbidden(*_args, **_kwargs):  # noqa: ANN202
        nonlocal called
        called = True

    monkeypatch.setattr(curation, "status", forbidden)
    with pytest.raises(ValueError, match="INVALID_CURATION_ACTION"):
        commands.op_maintain_memory(
            vault, mode="curation", curation_action="future-action", run_id="r"
        )
    with pytest.raises(ValueError, match="INVALID_ARGUMENTS"):
        commands.op_maintain_memory(
            vault,
            mode="curation",
            curation_action="status",
            run_id="r",
            categories=["stale"],
        )
    assert called is False


def test_curation_completed_replay_uses_shared_noop_terminal(tmp_path: Path) -> None:
    from exomem.command_surface import Command

    replay = {
        "run_id": "cur-20260901-aaaaaaaaaaaa",
        "plan_id": "a" * 64,
        "plan_fingerprint": "b" * 64,
        "phase": "completed",
        "committed_steps": ["one"],
        "receipts": [],
        "next_action": None,
        "outcome": "replayed",
        "mutated": False,
    }
    command = Command(
        name="maintain_memory",
        leaf=lambda _root, **_kwargs: replay,
        params=(),
        surfaces=frozenset({"mcp"}),
        cli_writes=True,
    )

    result = LeaseManager(
        LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "state")})
    ).invoke(
        command,
        (tmp_path,),
        {"mode": "curation", "curation_action": "resume"},
    )

    assert result["status"] == "replayed"
    assert result["mutated"] is False
    assert result["outcome"] == "replayed"


def test_standalone_writer_executes_curation_with_digest_operation_identity(
    vault: Path, tmp_path: Path
) -> None:
    command = _command()
    manager = LeaseManager(
        LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "state")})
    )
    plan = {
        "version": 1,
        "title": "Monikielinen hyväksytty suunnitelma",
        "steps": [
            {
                "step_id": "unicode",
                "kind": "create-note",
                "args": {
                    "title": "Monikielinen muisti",
                    "slug": "standalone-curation-unicode",
                    "content": (
                        "## Observations\n\n"
                        "- [finding] Säilytä tämä täsmälleen — 記憶を守る。 ^unicode\n"
                    ),
                    "relation_disposition": "reviewed_none",
                    "relation_review_reason": (
                        "No honest relation exists for this isolated fixture."
                    ),
                },
            }
        ],
    }

    proposed = manager.invoke(
        command,
        (vault,),
        {"mode": "curation", "curation_action": "propose", "plan": plan},
        read_only=False,
        idempotency_key="standalone-curation-propose",
    )
    applied = manager.invoke(
        command,
        (vault,),
        {
            "mode": "curation",
            "curation_action": "apply",
            "run_id": proposed["run_id"],
            "plan_id": proposed["plan_id"],
            "expected_plan_fingerprint": proposed["plan_fingerprint"],
            "why": "Approve the immutable multilingual plan.",
        },
        read_only=False,
        idempotency_key="standalone-curation-apply",
    )

    assert applied["phase"] == "completed"
    assert len(applied["operation_id"]) == 64
    assert (
        vault / "Knowledge Base/Notes/Insights/standalone-curation-unicode.md"
    ).read_text(encoding="utf-8").endswith(
        "- [finding] Säilytä tämä täsmälleen — 記憶を守る。 ^unicode\n"
    )
