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


def test_save_roles_writes_the_override_and_its_snapshot_through_the_canonical_batch(
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
    # One canonical batch: the override and a snapshot of what it replaced
    # (this bare vault has no log.md to prepend to), nothing else.
    assert len(calls) == 1
    assert len(calls[0]) == 2
    write, snapshot = calls[0]
    assert write.path == context_roles.override_path(bare_vault)
    assert snapshot.create_only is True
    assert snapshot.path.parent.parent == bare_vault / "Knowledge Base" / "_Schema" / "history"
    assert yaml.safe_load(write.content) == _valid_roles_proposal()


def test_save_conventions_writes_the_override_and_its_snapshot_through_the_canonical_batch(
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
    # One canonical batch: the override and a snapshot of what it replaced
    # (this bare vault has no log.md to prepend to), nothing else.
    assert len(calls) == 1
    assert len(calls[0]) == 2
    write, snapshot = calls[0]
    assert write.path == ac.override_path(bare_vault)
    assert snapshot.create_only is True
    assert snapshot.path.parent.parent == bare_vault / "Knowledge Base" / "_Schema" / "history"
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


# --------------------------------------------------------------------------- #
# History and restore (close-memory-loop step 5, task B2): every governed save
# leaves its why and both hashes in log.md and a snapshot of what it replaced,
# and `history`/`restore` read and reinstate those versions.
# --------------------------------------------------------------------------- #


def _roles_proposal(cue: str) -> dict[str, object]:
    return {"schema_version": 1, "roles": {"constraints": {"add_cues": [cue]}}}


def _conventions_proposal(folder: str) -> dict[str, object]:
    return {"schema_version": 1, "anchors": {"add_skip_folders": [folder]}}


_SUBJECTS = {
    "context-roles": ("save-roles", _roles_proposal, lambda root: _current_roles_hash(root)),
    "activation-conventions": (
        "save-conventions",
        _conventions_proposal,
        lambda root: ac.load_conventions(root).content_hash,
    ),
}


def _governed_save(root: Path, subject: str, value: str, why: str) -> dict:
    operation, proposal, current = _SUBJECTS[subject]
    return commands.op_schema_memory(
        root,
        subject=subject,
        operation=operation,
        proposal=proposal(value),
        why=why,
        expected_hash=current(root),
    )


def _history(root: Path, subject: str) -> list[dict]:
    return commands.op_schema_memory(root, subject=subject, operation="history")["versions"]


@pytest.mark.parametrize("subject", sorted(_SUBJECTS))
def test_a_governed_save_logs_its_why_and_hashes(vault: Path, subject: str) -> None:
    context_roles.clear_cache()
    ac.clear_cache()
    before = _SUBJECTS[subject][2](vault)

    result = _governed_save(vault, subject, "ceilingword", "the user names limits this way")

    after = result["saved"]["content_hash"]
    log_text = (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")
    operation = _SUBJECTS[subject][0]
    assert (
        f"schema_memory {operation}: the user names limits this way ({before[:8]} -> {after[:8]})"
        in log_text
    )
    versions = _history(vault, subject)
    assert versions[0]["why"] == "the user names limits this way"
    assert versions[0]["before_hash"] == before[:8]
    assert versions[0]["after_hash"] == after[:8]
    snapshot = vault / versions[0]["path"]
    assert snapshot.is_file()
    assert snapshot.parent == vault / "Knowledge Base" / "_Schema" / "history" / snapshot.parent.name


@pytest.mark.parametrize("subject", sorted(_SUBJECTS))
def test_restore_returns_a_registry_to_a_previous_version(vault: Path, subject: str) -> None:
    context_roles.clear_cache()
    ac.clear_cache()
    current = _SUBJECTS[subject][2]
    _governed_save(vault, subject, "firstword", "first")
    first_hash = current(vault)
    _governed_save(vault, subject, "secondword", "second")
    assert current(vault) != first_hash
    # The newest version is the state the second save replaced: the first.
    version = _history(vault, subject)[0]["version"]

    restored = commands.op_schema_memory(
        vault,
        subject=subject,
        operation="restore",
        version=version,
        why="the second change was wrong",
        expected_hash=current(vault),
    )

    assert restored["valid"] is True
    assert current(vault) == first_hash
    # A restore is itself a governed save: history grows, nothing is lost.
    history = _history(vault, subject)
    assert history[0]["why"] == "the second change was wrong"
    assert len(history) == 3


def test_restore_refuses_a_stale_hash(bare_vault: Path) -> None:
    _governed_save(bare_vault, "activation-conventions", "Vorlagen", "first")
    version = _history(bare_vault, "activation-conventions")[0]["version"]
    stale = ac.load_conventions(bare_vault).content_hash
    _governed_save(bare_vault, "activation-conventions", "Modelle", "second")
    on_disk = ac.override_path(bare_vault).read_bytes()

    with pytest.raises(ValueError, match="STALE_ACTIVATION_CONVENTIONS_REGISTRY"):
        commands.op_schema_memory(
            bare_vault,
            subject="activation-conventions",
            operation="restore",
            version=version,
            why="undo",
            expected_hash=stale,
        )
    assert ac.override_path(bare_vault).read_bytes() == on_disk


def test_restore_requires_why_and_a_known_version(bare_vault: Path) -> None:
    _governed_save(bare_vault, "context-roles", "ceilingword", "first")
    current = _current_roles_hash(bare_vault)
    with pytest.raises(ValueError, match="WHY_REQUIRED"):
        commands.op_schema_memory(
            bare_vault, subject="context-roles", operation="restore",
            version=_history(bare_vault, "context-roles")[0]["version"], expected_hash=current,
        )
    with pytest.raises(ValueError, match="UNKNOWN_REGISTRY_VERSION"):
        commands.op_schema_memory(
            bare_vault, subject="context-roles", operation="restore",
            version="../../etc/passwd", why="undo", expected_hash=current,
        )


def test_history_keeps_the_newest_twenty(bare_vault: Path) -> None:
    for index in range(23):
        _governed_save(bare_vault, "context-roles", f"cueword{index}", f"save {index}")

    history = _history(bare_vault, "context-roles")
    snapshots = sorted(
        (bare_vault / "Knowledge Base" / "_Schema" / "history" / "context-roles").glob("*.yaml")
    )
    assert len(snapshots) == 20
    assert len(history) == 20
    assert history[0]["why"] == "save 22"
    assert [item["version"] for item in history] == sorted(
        (item["version"] for item in history), reverse=True
    )
