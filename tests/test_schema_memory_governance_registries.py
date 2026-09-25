"""Task 4.1/4.2 — the governed write path for the two vault-owned registries.

`context-roles` and `activation-conventions` gain a write path through
`schema_memory`, on the `save-relations` pattern (design.md decision 7):
`validate`/`diff` are read-only, `save-roles`/`save-conventions` require
`proposal`, `why` and `expected_hash` and refuse the generic `save` flag, a
proposal holding any finding is never saved, and `infer` is refused outright
because the server does not propose conventions or roles.

Every write in this file goes through `commands.op_schema_memory` -- the tool
entry point -- never `Path.write_text`. The acceptance is the scenarios in
`specs/context-roles/spec.md` and `specs/activation-conventions/spec.md`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from exomem import activation_conventions as ac
from exomem import commands, context_roles, vault


@pytest.fixture
def bare_vault(tmp_path: Path) -> Path:
    """A vault root with no vault-authored overrides yet."""
    root = tmp_path / "vault"
    root.mkdir()
    context_roles.clear_cache()
    ac.clear_cache()
    return root


def _current_roles_hash(vault_root: Path) -> str:
    return context_roles.load_roles(vault_root).roles_hash


def _current_conventions_hash(vault_root: Path) -> str:
    return ac.load_conventions(vault_root).content_hash


def _valid_roles_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "roles": {
            "constraints": {"add_cues": ["ceiling"]},
        },
    }


def _valid_conventions_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "anchors": {"add_skip_folders": ["Vorlagen"]},
    }


# --------------------------------------------------------------------------- #
# infer is refused outright
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("subject", ["context-roles", "activation-conventions"])
def test_infer_is_refused(bare_vault: Path, subject: str) -> None:
    with pytest.raises(ValueError, match="INVALID_SCHEMA_OPERATION"):
        commands.op_schema_memory(bare_vault, subject=subject, operation="infer")


# --------------------------------------------------------------------------- #
# validate: findings and the CURRENT content hash, nothing written
# --------------------------------------------------------------------------- #


def test_validate_context_roles_reports_findings_and_current_hash(bare_vault: Path) -> None:
    before_hash = _current_roles_hash(bare_vault)
    proposal = {
        "schema_version": 1,
        "roles": {"constraints": {"lane": "not-a-real-lane"}},
    }

    result = commands.op_schema_memory(
        bare_vault, subject="context-roles", operation="validate", proposal=proposal
    )

    assert result["valid"] is False
    assert result["findings"]
    assert result["content_hash"] == before_hash
    assert not context_roles.override_path(bare_vault).exists()


def test_validate_conventions_reports_the_rejected_folder_rule(bare_vault: Path) -> None:
    before_hash = _current_conventions_hash(bare_vault)
    proposal = {
        "schema_version": 1,
        "anchors": {"hub": {"add_folders": ["Sources/Articles"]}},
    }

    result = commands.op_schema_memory(
        bare_vault, subject="activation-conventions", operation="validate", proposal=proposal
    )

    assert result["valid"] is False
    assert any(f["code"] == "rule_append_only" for f in result["findings"])
    assert result["content_hash"] == before_hash
    assert not ac.override_path(bare_vault).exists()


# --------------------------------------------------------------------------- #
# diff: the proposal against the effective registry
# --------------------------------------------------------------------------- #


def test_diff_context_roles_against_the_effective_registry(bare_vault: Path) -> None:
    result = commands.op_schema_memory(
        bare_vault,
        subject="context-roles",
        operation="diff",
        proposal=_valid_roles_proposal(),
    )

    assert result["subject"] == "context-roles"
    assert result["changed"] is True
    assert result["content_hash"] == _current_roles_hash(bare_vault)


def test_diff_conventions_against_the_effective_registry(bare_vault: Path) -> None:
    result = commands.op_schema_memory(
        bare_vault,
        subject="activation-conventions",
        operation="diff",
        proposal=_valid_conventions_proposal(),
    )

    assert result["subject"] == "activation-conventions"
    assert result["changed"] is True
    assert result["content_hash"] == _current_conventions_hash(bare_vault)


# --------------------------------------------------------------------------- #
# save-roles / save-conventions: the governed write
# --------------------------------------------------------------------------- #


def test_save_roles_writes_only_the_override_file_through_the_canonical_batch(
    bare_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[vault.PlannedWrite]] = []

    def batch(writes, *, vault_root: Path):  # noqa: ANN001
        calls.append(list(writes))
        return [write.path for write in writes]

    monkeypatch.setattr(vault, "batch_atomic_write", batch)
    before_hash = _current_roles_hash(bare_vault)

    result = commands.op_schema_memory(
        bare_vault,
        subject="context-roles",
        operation="save-roles",
        proposal=_valid_roles_proposal(),
        why="add a budget-ceiling cue",
        expected_hash=before_hash,
    )

    assert result["valid"] is True
    assert result["saved"]["previous_hash"] == before_hash
    assert len(calls) == 1
    assert len(calls[0]) == 1
    write = calls[0][0]
    assert write.path == context_roles.override_path(bare_vault)
    assert yaml.safe_load(write.content) == _valid_roles_proposal()


def test_save_conventions_writes_only_the_override_file_through_the_canonical_batch(
    bare_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[vault.PlannedWrite]] = []

    def batch(writes, *, vault_root: Path):  # noqa: ANN001
        calls.append(list(writes))
        return [write.path for write in writes]

    monkeypatch.setattr(vault, "batch_atomic_write", batch)
    before_hash = _current_conventions_hash(bare_vault)

    result = commands.op_schema_memory(
        bare_vault,
        subject="activation-conventions",
        operation="save-conventions",
        proposal=_valid_conventions_proposal(),
        why="this vault spells its templates folder differently",
        expected_hash=before_hash,
    )

    assert result["valid"] is True
    assert result["saved"]["previous_hash"] == before_hash
    assert len(calls) == 1
    assert len(calls[0]) == 1
    write = calls[0][0]
    assert write.path == ac.override_path(bare_vault)
    assert yaml.safe_load(write.content) == _valid_conventions_proposal()


def test_an_agent_adds_a_cue_through_the_governed_tool_and_the_next_read_sees_it(
    bare_vault: Path,
) -> None:
    before_hash = _current_roles_hash(bare_vault)

    saved = commands.op_schema_memory(
        bare_vault,
        subject="context-roles",
        operation="save-roles",
        proposal=_valid_roles_proposal(),
        why="add a budget-ceiling cue",
        expected_hash=before_hash,
    )

    assert saved["valid"] is True
    reloaded = context_roles.load_roles(bare_vault)
    assert reloaded.source == "vault"
    assert reloaded.roles_hash != before_hash
    assert "ceiling" in reloaded.roles["constraints"].cues


def test_save_refuses_the_generic_save_flag(bare_vault: Path) -> None:
    with pytest.raises(ValueError):
        commands.op_schema_memory(
            bare_vault,
            subject="context-roles",
            operation="save-roles",
            proposal=_valid_roles_proposal(),
            why="probe",
            expected_hash=_current_roles_hash(bare_vault),
            save=True,
        )


@pytest.mark.parametrize(
    "subject, operation, proposal_factory",
    [
        ("context-roles", "save-roles", _valid_roles_proposal),
        ("activation-conventions", "save-conventions", _valid_conventions_proposal),
    ],
)
def test_save_requires_why_and_expected_hash(
    bare_vault: Path, subject: str, operation: str, proposal_factory
) -> None:
    with pytest.raises(ValueError, match="WHY_REQUIRED"):
        commands.op_schema_memory(
            bare_vault,
            subject=subject,
            operation=operation,
            proposal=proposal_factory(),
            expected_hash="whatever",
        )
    with pytest.raises(ValueError, match="EXPECTED_HASH_REQUIRED"):
        commands.op_schema_memory(
            bare_vault,
            subject=subject,
            operation=operation,
            proposal=proposal_factory(),
            why="probe",
        )


def test_save_roles_refuses_the_wrong_operation_for_the_subject(bare_vault: Path) -> None:
    with pytest.raises(ValueError, match="INVALID_SCHEMA_OPERATION"):
        commands.op_schema_memory(
            bare_vault,
            subject="context-roles",
            operation="save-conventions",
            proposal=_valid_roles_proposal(),
            why="probe",
            expected_hash=_current_roles_hash(bare_vault),
        )


# --------------------------------------------------------------------------- #
# A stale hash refuses the save
# --------------------------------------------------------------------------- #


def test_a_stale_roles_hash_refuses_the_save(bare_vault: Path) -> None:
    before_hash = _current_roles_hash(bare_vault)
    commands.op_schema_memory(
        bare_vault,
        subject="context-roles",
        operation="save-roles",
        proposal=_valid_roles_proposal(),
        why="first save",
        expected_hash=before_hash,
    )

    with pytest.raises(ValueError, match="STALE"):
        commands.op_schema_memory(
            bare_vault,
            subject="context-roles",
            operation="save-roles",
            proposal={"schema_version": 1, "roles": {"constraints": {"add_cues": ["floor"]}}},
            why="second save with a stale hash",
            expected_hash=before_hash,
        )
    assert "floor" not in context_roles.load_roles(bare_vault).roles["constraints"].cues


def test_a_stale_conventions_hash_refuses_the_save(bare_vault: Path) -> None:
    before_hash = _current_conventions_hash(bare_vault)
    commands.op_schema_memory(
        bare_vault,
        subject="activation-conventions",
        operation="save-conventions",
        proposal=_valid_conventions_proposal(),
        why="first save",
        expected_hash=before_hash,
    )

    with pytest.raises(ValueError, match="STALE"):
        commands.op_schema_memory(
            bare_vault,
            subject="activation-conventions",
            operation="save-conventions",
            proposal={"schema_version": 1, "anchors": {"add_skip_folders": ["Entwurf"]}},
            why="second save with a stale hash",
            expected_hash=before_hash,
        )
    assert "Entwurf" not in ac.load_conventions(bare_vault).conventions.skip_folders


# --------------------------------------------------------------------------- #
# A proposal with a finding is not saved
# --------------------------------------------------------------------------- #


def test_save_conventions_refuses_a_proposal_with_any_finding(bare_vault: Path) -> None:
    before_hash = _current_conventions_hash(bare_vault)
    proposal = {
        "schema_version": 1,
        "anchors": {"hub": {"add_folders": ["Sources/Articles"]}},
    }

    result = commands.op_schema_memory(
        bare_vault,
        subject="activation-conventions",
        operation="save-conventions",
        proposal=proposal,
        why="probe",
        expected_hash=before_hash,
    )

    assert result["valid"] is False
    assert any(f["code"] == "rule_append_only" for f in result["findings"])
    assert result["saved"] is None
    assert not ac.override_path(bare_vault).exists()


def test_save_roles_refuses_a_proposal_with_any_finding(bare_vault: Path) -> None:
    before_hash = _current_roles_hash(bare_vault)
    proposal = {"schema_version": 1, "roles": {"constraints": {"lane": "not-a-real-lane"}}}

    result = commands.op_schema_memory(
        bare_vault,
        subject="context-roles",
        operation="save-roles",
        proposal=proposal,
        why="probe",
        expected_hash=before_hash,
    )

    assert result["valid"] is False
    assert result["findings"]
    assert result["saved"] is None
    assert not context_roles.override_path(bare_vault).exists()


# --------------------------------------------------------------------------- #
# Task 5.4 — three-door parity: MCP, CLI and REST reach the same leaf
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "subject, request_kwargs",
    [
        (
            "context-roles",
            {"subject": "context-roles", "operation": "validate", "proposal": _valid_roles_proposal()},
        ),
        (
            "activation-conventions",
            {
                "subject": "activation-conventions",
                "operation": "validate",
                "proposal": _valid_conventions_proposal(),
            },
        ),
    ],
)
def test_validate_matches_across_mcp_cli_and_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    subject: str,
    request_kwargs: dict[str, object],
) -> None:
    """`schema_memory` is registered once for every door (design.md decision
    7 adds no per-door wiring); this pins that a `validate` call on each new
    subject reaches the SAME leaf and returns the SAME bytes through all
    three, the way `test_entity_type_resolution_schema_and_dispatch_match_
    across_surfaces` already pins it for `entity-types`.
    """
    from conftest import initialize_vault_state_offline

    from exomem import server as server_module
    from exomem.__main__ import main
    from exomem.init import init_vault

    bare = tmp_path / "vault"
    init_vault(bare)
    initialize_vault_state_offline(bare, source="schema_memory governance parity")
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(bare))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")

    direct = commands.op_schema_memory(bare, **request_kwargs)
    mcp = server_module.build_server(require_auth=False)
    mcp_result = asyncio.run(mcp.call_tool("schema_memory", request_kwargs, run_middleware=True))
    rest = TestClient(mcp.http_app()).post(
        "/api/schema_memory",
        json=request_kwargs,
        headers={"Authorization": "Bearer sekret"},
    )

    assert mcp_result.structured_content == direct
    assert rest.status_code == 200, rest.text
    assert rest.json() == {"success": True, "data": direct}

    cli_args = ["schema_memory", "--subject", subject, "--operation", "validate", "--json"]
    if subject == "context-roles":
        cli_args += ["--proposal", json.dumps(_valid_roles_proposal())]
    else:
        cli_args += ["--proposal", json.dumps(_valid_conventions_proposal())]
    code = main(cli_args)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"success": True, "data": direct}
