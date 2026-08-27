"""Governance authoring core: deterministic routing and operation coverage."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from exomem import commands, reserved_paths
from exomem.command_surface import Command
from exomem.governance import store
from exomem.governance.principal import RequestPrincipal, owner_principal

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
PATTERN_GLOB = "Knowledge Base/Notes/Patterns/**"
V4_NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _governance_dispatcher_authority():
    with reserved_paths._owner_authority_scope("govern_memory"):
        yield


def test_recovery_imports_cleanly_before_the_governance_tool() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import exomem.governance.recovery"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_recovery_import_does_not_load_the_governance_tool() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import exomem.governance.recovery; "
            "raise SystemExit('exomem.governance.tool' in sys.modules)",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _proposal_documents(*, ceiling: int = 1) -> dict[str, str]:
    return {
        "scopes/confidential-patterns.yaml": (
            "governance_version: 1\n"
            f"id: {SCOPE_ID}\n"
            "name: Confidential patterns\n"
            f'paths: ["{PATTERN_GLOB}"]\n'
        ),
        "rules/confidential-patterns.yaml": (
            "governance_version: 1\n"
            f"id: {RULE_ID}\n"
            f'scope_ids: ["{SCOPE_ID}"]\n'
            "audience: external\n"
            f"ceiling: {ceiling}\n"
        ),
    }


def _propose(vault: Path, *, ceiling: int = 1) -> dict:
    from exomem.governance.tool import op_govern_memory

    return op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Treat pattern notes as confidential for the external audience",
        documents=_proposal_documents(ceiling=ceiling),
        selector_paths=[PATTERN_GLOB],
        target_ceiling=ceiling,
        duration="standing",
    )


def _command() -> Command:
    from exomem.governance.tool import op_govern_memory

    return Command(
        name="govern_memory",
        leaf=op_govern_memory,
        params=(),
        surfaces=frozenset(),
        tier=2,
        cli_writes=True,
    )


def test_operation_registry_drives_read_write_and_receipt_mapping() -> None:
    from exomem.governance.tool import (
        OPERATION_SPECS,
        OperationSpec,
        assert_operation_coverage,
    )

    assert isinstance(OPERATION_SPECS, dict) is False
    assert {name for name, spec in OPERATION_SPECS.items() if spec.read_only} == {
        "list",
        "explain",
        "simulate",
    }
    assert {
        name for name, spec in OPERATION_SPECS.items() if spec.authorization_affecting
    } == {"commit", "grant", "revoke", "suspend", "resume", "undo", "declare"}
    assert OPERATION_SPECS["propose"].authorization_exemption is True
    assert all(spec.handler_key for spec in OPERATION_SPECS.values())
    assert not hasattr(commands, "_GOVERN_MEMORY_ACTIONS")
    assert not hasattr(commands, "_GOVERN_MEMORY_READ_ONLY_ACTIONS")
    assert_operation_coverage()
    with pytest.raises(RuntimeError, match="lacks receipt coverage"):
        assert_operation_coverage(
            {"future": OperationSpec(False, "owner", authorization_affecting=True)}
        )


def test_standing_variants_are_fully_covered_by_the_single_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import operations

    assert not hasattr(operations, "_INTERNAL_RECOVERY")
    grant_variants = getattr(operations.OPERATION_SPECS["grant"], "variants", ())
    revoke_variants = getattr(operations.OPERATION_SPECS["revoke"], "variants", ())
    assert grant_variants and revoke_variants
    standing_grant = next(item for item in grant_variants if item.mode == "standing")
    standing_revoke = next(item for item in revoke_variants if item.mode == "standing")
    assert standing_grant.journal_operation == "standing_grant"
    assert standing_revoke.journal_operation == "standing_revoke"
    assert standing_grant.yaml_marker and standing_revoke.yaml_marker
    assert operations.journal_variant("standing_grant") is standing_grant
    assert operations.journal_variant("standing_revoke") is standing_revoke

    drifted = dataclasses.replace(standing_grant, yaml_marker=False)
    with pytest.raises(RuntimeError, match="standing variant metadata drift"):
        operations.assert_operation_coverage(
            {
                "grant": dataclasses.replace(
                    operations.OPERATION_SPECS["grant"], variants=(drifted,)
                )
            }
        )
    monkeypatch.setattr(
        operations,
        "_CONSUMED_VARIANT_FIELDS",
        operations._CONSUMED_VARIANT_FIELDS - {"recovery_policy"},
    )
    with pytest.raises(RuntimeError, match="unconsumed operation variant metadata"):
        operations.assert_operation_coverage()


def test_registry_dispatches_handlers_and_registered_recovery_strategies(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import operations
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    recovery_module = importlib.import_module("exomem.governance.recovery")
    standing = operations.operation_variant("grant", "standing")
    assert standing.handler_key == "grant_standing"
    assert standing.authorization == "owner"
    assert operations.operation_variant("grant").handler_key == "grant_session"
    assert operations.recovery_strategy(standing.recovery_policy).component_kinds == {
        "archive",
        "proposal",
        "proposal_guard",
        "yaml",
    }

    calls: list[str] = []

    def selected_handler(_vault: Path, **kwargs):
        calls.append(kwargs["_selection"].handler_key)
        return {"status": "selected"}

    monkeypatch.setattr(
        governance_tool,
        "_HANDLER_STRATEGIES",
        {
            **governance_tool._HANDLER_STRATEGIES,
            "grant_standing": selected_handler,
        },
    )
    assert op_govern_memory(
        vault,
        operation="grant",
        scope="standing",
        principal=owner_principal(),
    ) == {"status": "selected"}
    assert calls == ["grant_standing"]

    spec = operations.OPERATION_SPECS["grant"]
    with pytest.raises(RuntimeError, match="invalid governance handler strategy"):
        operations.assert_operation_coverage(
            {"grant": dataclasses.replace(spec, handler_key="missing")}
        )
    with pytest.raises(RuntimeError, match="invalid governance recovery strategy"):
        operations.assert_operation_coverage(
            {
                "grant": dataclasses.replace(
                    spec,
                    variants=(
                        dataclasses.replace(standing, recovery_policy="missing"),
                    ),
                )
            }
        )
    with pytest.raises(RuntimeError, match="variant journal operation drift"):
        operations.assert_operation_coverage(
            {
                "grant": dataclasses.replace(
                    spec,
                    variants=(
                        dataclasses.replace(
                            standing, journal_operation="renamed_standing_grant"
                        ),
                    ),
                )
            }
        )
    with pytest.raises(RuntimeError, match="duplicate governance operation variant"):
        operations.assert_operation_coverage(
            {"grant": dataclasses.replace(spec, variants=(standing, standing))}
        )
    unregistered = dataclasses.replace(
        standing,
        mode="temporary",
        journal_operation="temporary_grant",
        handler_key="grant_standing",
        recovery_policy="compound_grant",
    )
    with pytest.raises(RuntimeError, match="variant handler strategy drift"):
        operations.assert_operation_coverage(
            {"grant": dataclasses.replace(spec, variants=(unregistered,))}
        )

    _committed_policy(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="suspend",
            principal=owner_principal(),
            rule_ids=[RULE_ID],
            crash_at="after_intent",
        )
    real_transition = operations.journal_variant("suspend")
    monkeypatch.setattr(
        recovery_module,
        "journal_variant",
        lambda _operation: dataclasses.replace(
            real_transition, recovery_policy="compound_grant"
        ),
    )
    assert recovery_module.reconcile_governance_operations(vault)["blocked"] is True


def test_registry_destructive_and_child_receipt_metadata_drive_runtime(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import egress, operations, receipts, tokens
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import op_govern_memory

    altered = {
        **operations.OPERATION_SPECS,
        "grant": dataclasses.replace(
            operations.OPERATION_SPECS["grant"],
            child_receipts=("registry_token", "registry_grant"),
        ),
        "revoke": dataclasses.replace(
            operations.OPERATION_SPECS["revoke"], destructive=False
        ),
    }
    registry = MappingProxyType(altered)
    monkeypatch.setattr(operations, "OPERATION_SPECS", registry)
    monkeypatch.setattr(governance_tool, "OPERATION_SPECS", registry)

    assert not operations.is_destructive("revoke")
    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=egress.LEVEL_EXCERPT,
        authorization_session="conversation-a",
    )
    grant = op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )
    children = {
        row["operation"]
        for row in receipts.event_records(vault)
        if row.get("parent_causation_id") == grant["causation_id"]
    }
    assert children == {"registry_token", "registry_grant"}


@pytest.mark.parametrize("operation", ["list", "explain", "simulate"])
def test_inspection_invocations_are_read_only(operation: str) -> None:
    assert commands.invocation_is_read_only(_command(), {"operation": operation})


@pytest.mark.parametrize(
    "operation",
    ["propose", "commit", "grant", "revoke", "suspend", "resume", "undo", "declare"],
)
def test_authoring_invocations_require_the_writer(operation: str) -> None:
    assert not commands.invocation_is_read_only(_command(), {"operation": operation})


def test_unknown_operation_fails_before_creating_sidecar(vault: Path) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    with pytest.raises(GovernanceError, match="UNKNOWN_GOVERNANCE_OPERATION"):
        op_govern_memory(vault, operation="future-operation")
    assert not store.sidecar_path(vault).exists()


def test_grant_rejects_unknown_scope_before_creating_sidecar(vault: Path) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    with pytest.raises(GovernanceError) as error:
        op_govern_memory(vault, operation="grant", scope="typo")
    assert error.value.code == "INVALID_GRANT_SCOPE"
    assert not store.sidecar_path(vault).exists()


def test_govern_memory_propose_defaults_to_a_full_committed_terminal(vault: Path) -> None:
    from exomem.governance.principal import request_scope
    from exomem.writer_lease import LeaseConfig, LeaseManager

    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "govern_memory")
    manager = LeaseManager(LeaseConfig(state_dir=vault.parent / "writer-state"))
    proposal = {
        "operation": "propose",
        "documents": _proposal_documents(),
        "selector_paths": [PATTERN_GLOB],
        "intent": "Treat pattern notes as confidential for the external audience",
        "target_ceiling": 1,
        "duration": "standing",
    }

    with request_scope(owner_principal()):
        full = manager.invoke(
            command,
            (vault,),
            proposal,
            idempotency_key="govern-propose",
        )
        explicit_full = manager.invoke(
            command,
            (vault,),
            {**proposal, "response_detail": "full"},
            idempotency_key="govern-propose",
        )
        compact = manager.invoke(
            command,
            (vault,),
            {**proposal, "response_detail": "compact"},
            idempotency_key="govern-propose",
        )
        legacy = manager.invoke(
            command,
            (vault,),
            {**proposal, "response_detail": "legacy"},
            idempotency_key="govern-propose",
        )

    diagnostics = full["diagnostics"]
    assert {
        "interpretation",
        "canonical_yaml",
        "membership_preview",
        "consequences",
        "overlaps",
        "duration",
        "reversal",
        "proposal_id",
    } <= diagnostics.keys()
    assert explicit_full == full
    assert compact["status"] == "committed"
    assert "diagnostics" not in compact
    assert legacy == diagnostics
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0] == 1

    with request_scope(owner_principal()):
        committed = manager.invoke(
            command,
            (vault,),
            {"operation": "commit", "proposal_id": diagnostics["proposal_id"]},
        )
    assert committed["diagnostics"]["proposal_id"] == diagnostics["proposal_id"]


def test_ungoverned_inspection_stays_frictionless_and_noncreating(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    result = op_govern_memory(
        vault,
        operation="list",
        principal=owner_principal(),
    )
    assert result == {"enabled": False, "rules": [], "scopes": [], "grants": []}
    assert not store.sidecar_path(vault).exists()


def test_v3_store_owns_tools_schema_and_uses_full_durability(vault: Path) -> None:
    conn = store.open_connection(vault)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "withhold_tokens",
            "governance_proposals",
            "governance_operation_journals",
            "governance_operation_components",
            "governance_session_grants",
            "governance_session_purpose",
        } <= tables
    finally:
        conn.close()


def test_v2_migrates_to_v3_once_and_future_authoring_refuses(vault: Path) -> None:
    path = store.sidecar_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=2")
    migrated = store.open_connection(vault)
    migrated.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        conn.execute("PRAGMA user_version=5")
    future = store.open_connection(vault)
    future.close()
    with pytest.raises(store.UnsupportedGovernanceSchema):
        store.require_authoring_schema(vault)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_policy_loader_reads_existing_v2_without_migration_or_ddl(vault: Path) -> None:
    from exomem.governance import policy

    path = store.sidecar_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=2")
    loaded = policy.load(vault)
    assert loaded.empty and not loaded.blocked
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "governance_operation_journals" not in tables


def test_request_principal_carries_explicit_authorization_session() -> None:
    principal = RequestPrincipal(
        audience_id="external",
        surface="mcp",
        authorization_session_id="conversation-opaque-handle",
    )
    assert principal.authorization_session_id == "conversation-opaque-handle"


def test_propose_is_deterministic_private_and_persists_exact_membership(vault: Path) -> None:
    proposal = _propose(vault)
    assert proposal["interpretation"] == (
        "Treat pattern notes as confidential for the external audience"
    )
    assert proposal["canonical_yaml"] == _proposal_documents()
    assert proposal["membership_preview"]["count"] >= 1
    assert proposal["membership_preview"]["samples"] == []
    assert proposal["consequences"]["target_ceiling"] == 1
    assert proposal["reversal"] == "undo"
    assert proposal["proposal_id"]
    # A proposal that would restrict these pages cannot leak their current
    # titles, excerpts, or paths through its preview.
    preview = str(proposal["membership_preview"])
    assert "kill-switch" not in preview
    assert "retry-with" not in preview

    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        row = conn.execute(
            "SELECT status, membership_manifest FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()
    assert row is not None and row[0] == "pending"
    assert "kill-switch-for-risky-releases.md" in row[1]


def test_commit_refuses_membership_or_content_drift_without_intent(vault: Path) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import GovernanceError, op_govern_memory

    proposal = _propose(vault)
    added = vault / "Knowledge Base" / "Notes" / "Patterns" / "added-after-propose.md"
    added.write_text("# Added after proposal\n", encoding="utf-8")

    with pytest.raises(GovernanceError) as err:
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert err.value.code == "STALE_GOVERNANCE_POLICY"
    assert not receipts.event_records(vault)
    assert not (vault / "Knowledge Base" / "_Governance" / "scopes").exists()


def test_commit_spends_once_after_terminal_and_uses_distinct_attempt_identity(
    vault: Path,
) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        GovernanceError,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_intent",
        )
    first = reconcile_governance_operations(vault)
    assert first["aborted"] == 1

    committed = op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    assert committed["status"] == "committed"
    assert committed["event_id"] != first["event_ids"][0]
    assert (
        vault / "Knowledge Base" / "_Governance" / "scopes" /
        "confidential-patterns.yaml"
    ).is_file()
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        status = conn.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()[0]
        archive_components = conn.execute(
            "SELECT COUNT(*) FROM governance_operation_components "
            "WHERE event_id=? AND component_kind='archive'",
            (committed["event_id"],),
        ).fetchone()[0]
    assert status == "spent"
    assert archive_components == 6
    with pytest.raises(GovernanceError) as err:
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert err.value.code == "PROPOSAL_SPENT"


def test_critical_identity_includes_prepared_digest(vault: Path) -> None:
    from exomem.governance import receipts

    first = receipts.begin_event(
        vault,
        operation="governance-test-a",
        prior="a" * 64,
        prepared="b" * 64,
        target="c" * 64,
    )
    receipts.abort_event(vault, first["event_id"])
    second = receipts.begin_event(
        vault,
        operation="governance-test-a",
        prior="a" * 64,
        prepared="d" * 64,
        target="c" * 64,
    )
    assert first["event_id"] != second["event_id"]


def test_commit_direction_is_proven_from_effective_rule_change(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    wider = _propose(vault, ceiling=2)
    assert op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=wider["proposal_id"],
    )["direction"] == "widening"
    narrower = _propose(vault, ceiling=1)
    assert op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=narrower["proposal_id"],
    )["direction"] == "narrowing"


def test_proposal_direction_treats_equal_level_option_change_as_unknown(
    vault: Path,
) -> None:
    from exomem.governance.tool import op_govern_memory

    original = _proposal_documents(ceiling=1)
    original["rules/confidential-patterns.yaml"] += (
        "options:\n  notice: Original notice\n"
    )
    proposed = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Install the original reviewed notice",
        documents=original,
        target_ceiling=1,
    )
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposed["proposal_id"],
    )
    changed = dict(original)
    changed["rules/confidential-patterns.yaml"] = changed[
        "rules/confidential-patterns.yaml"
    ].replace("Original notice", "Different notice")

    reviewed = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Review a different notice at the same ceiling",
        documents=changed,
        target_ceiling=1,
    )

    assert reviewed["consequences"]["direction"] == "widening"
    assert reviewed["consequences"]["widened"] > 0


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({"a": 5, "b": 3}, {"a": 4, "b": 3}, "narrowing"),
        ({"a": 2}, {"a": 3}, "widening"),
        ({"a": 2}, {"b": 1}, "widening"),
        (None, {"a": 1}, "widening"),
    ],
)
def test_transition_direction_requires_pointwise_proof(before, after, expected) -> None:
    from exomem.governance.tool import classify_transition_direction

    assert classify_transition_direction(before, after) == expected


def test_transition_direction_requires_equal_disclosure_at_equal_level() -> None:
    from exomem.governance.tool import classify_transition_direction

    old = (2, "old-disclosure")
    new = (2, "new-disclosure")
    assert classify_transition_direction({"a": old}, {"a": old}) == "narrowing"
    assert classify_transition_direction({"a": old}, {"a": new}) == "widening"
    assert classify_transition_direction({"a": old}, {"a": (1, new[1])}) == "narrowing"


def _external(session: str | None = "conversation-a") -> RequestPrincipal:
    return RequestPrincipal(
        audience_id="external",
        surface="mcp",
        authorization_session_id=session,
    )


def _committed_policy(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    proposal = _propose(vault)
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )


def _configure_v4_grant(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RequestPrincipal, object, str, str, str]:
    from exomem.governance import (
        authorization_session_authority,
        authorization_session_lifecycle,
        policy,
        schema_v4,
    )
    from exomem.governance import tool as governance_tool

    _committed_policy(vault)
    prospective = policy.compile_prospective(vault, {})
    assert prospective is not None and not prospective.policy.blocked
    compiled = prospective.policy
    seed = schema_v4.MigrationSeed(
        activation_store_id="activation-store-grant",
        logical_vault_id="logical-vault-grant",
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
            source_documents=prospective.target_documents,
            source_fingerprint=compiled.fingerprint,
            conflict_digest=prospective.snapshot.conflict_set_digest,
            compiled_policy=policy.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="authoring-v4-grant",
            receipt_event_id="receipt-v4-grant",
            created_at=V4_NOW,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=V4_NOW,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="namespace-v4-grant",
            evidence=b'{"ready":true}',
            ready_at=V4_NOW,
        ),
        migrated_at=V4_NOW,
    )
    connection = store.open_connection(vault)
    try:
        schema_v4.migrate_v3_connection(connection, seed)
        context = authorization_session_lifecycle.AuthorizationSessionContext(
            session_id="authorization-session:grant-recovery",
            principal_id="principal:external",
            issuer_family="mcp-oauth",
            cell_id="cell-grant",
            logical_vault_id="logical-vault-grant",
            keyring_id="keyring-grant",
            credential_generation=1,
            expires_at=V4_NOW + 600,
        )
        connection.execute(
            "INSERT INTO governance_authorization_sessions "
            "(session_id, locator_digest, verifier, verifier_key_id, "
            "credential_generation, principal_id, issuer_family, cell_id, "
            "logical_vault_id, keyring_id, status, created_at, rotated_at, "
            "expires_at, closed_at) VALUES (?, ?, ?, 'key-grant', 1, ?, ?, ?, ?, "
            "?, 'active', ?, NULL, ?, NULL)",
            (
                context.session_id,
                b"l" * 32,
                b"v" * 32,
                context.principal_id,
                context.issuer_family,
                context.cell_id,
                context.logical_vault_id,
                context.keyring_id,
                V4_NOW,
                context.expires_at,
            ),
        )
        connection.commit()
        path = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
        fingerprint = hashlib.sha256((vault / path).read_bytes()).hexdigest()
        signing_key = b"t" * 32
        token = authorization_session_authority.mint_escalation_token(
            connection,
            context=context,
            signing_key=signing_key,
            audience=context.principal_id,
            purpose=None,
            max_level=5,
            org_ceiling=6,
            paths=(path,),
            fingerprints=(fingerprint,),
            scope_ids=(SCOPE_ID,),
            now=V4_NOW + 1,
            expires_at=V4_NOW + 300,
        )
    finally:
        connection.close()

    principal = RequestPrincipal(
        audience_id=context.principal_id,
        surface="mcp",
        issuer_family=context.issuer_family,
        verified_authorization_session=context,
    )
    custody = SimpleNamespace(
        keyring=SimpleNamespace(
            accepted_keys=(SimpleNamespace(key=signing_key),),
        )
    )

    def authority_inputs(root: Path, _kwargs: object):
        return (
            principal,
            context,
            custody,
            store.open_authorization_session_connection(root),
            V4_NOW + 2,
        )

    monkeypatch.setattr(governance_tool, "_v4_authority_inputs", authority_inputs)
    monkeypatch.setattr(governance_tool.policy_module, "load", lambda _root: compiled)
    return principal, context, token, path, fingerprint


def test_grant_compound_receipts_leave_schema_v3_session_grant_inert_until_v4(
    vault: Path,
) -> None:
    from exomem.find_types import Hit
    from exomem.governance import egress, receipts, tokens
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=[
            "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
        ],
        audience="external",
        max_level=egress.LEVEL_EXCERPT,
        authorization_session="conversation-a",
    )
    granted = op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
        duration_seconds=600,
    )
    assert granted["status"] == "committed"
    records = receipts.event_records(vault)
    child_intents = [
        record
        for record in records
        if record.get("phase") == "intent"
        and str(record.get("operation", "")).startswith("governance_")
        and record.get("parent_causation_id") == granted["causation_id"]
    ]
    assert {record["operation"] for record in child_intents} == {
        "governance_token_redemption",
        "governance_grant_creation",
    }
    assert len({record["event_id"] for record in child_intents}) == 2

    rel_path = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    active, identity = store.active_session_grants(
        vault,
        audience="external",
        authorization_session="conversation-a",
        rel_path=rel_path,
        purpose=None,
    )
    assert active == []
    assert identity == "v3-session-grants-unscoped"

    result = egress.annotate_hits(
        vault,
        [
            Hit(
                path=rel_path,
                type="pattern",
                scope=None,
                title="restricted",
                updated="2026-01-01",
                excerpt="allowed excerpt",
            )
        ],
        principal=_external(),
        limit=1,
    )
    assert result.hits == []
    assert "allowed excerpt" not in repr(result)
    with pytest.raises(Exception, match="TOKEN_CONSUMED"):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
        )


def test_v4_grant_receipt_composite_persists_exact_reviewed_scope_ids(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import op_govern_memory

    principal, _context, token, _path, _fingerprint = _configure_v4_grant(
        vault, monkeypatch
    )

    granted = op_govern_memory(
        vault,
        operation="grant",
        principal=principal,
        token=token,
        duration_seconds=600,
    )

    assert granted["status"] == "committed"
    assert granted["causation_id"]
    records = receipts.event_records(vault)
    assert {
        record["operation"]
        for record in records
        if record.get("phase") == "intent"
        and record.get("parent_causation_id") == granted["causation_id"]
    } == {"governance_token_redemption", "governance_grant_creation"}
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        scope_ids, membership_manifest, status = connection.execute(
            "SELECT scope_ids, membership_manifest, status "
            "FROM governance_session_grants WHERE grant_id=?",
            (granted["grant_id"],),
        ).fetchone()
        component = connection.execute(
            "SELECT value_json FROM governance_operation_components "
            "WHERE event_id=? AND phase='final' AND component_kind='grant'",
            (granted["causation_id"],),
        ).fetchone()
    assert json.loads(scope_ids) == [SCOPE_ID]
    assert json.loads(membership_manifest)[0]["scope_ids"] == [SCOPE_ID]
    assert status == "active"
    assert component is not None
    projected = json.loads(component[0])
    assert projected["scope_ids"] == json.dumps([SCOPE_ID], separators=(",", ":"))
    assert json.loads(projected["membership_manifest"])[0]["scope_ids"] == [SCOPE_ID]


def test_v4_grant_crash_recovers_only_the_exact_scope_bound_prepared_state(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import authorization_session_authority
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    principal, context, token, path, fingerprint = _configure_v4_grant(
        vault, monkeypatch
    )
    with pytest.raises(GovernanceCrash, match="after_compound_state"):
        op_govern_memory(
            vault,
            operation="grant",
            principal=principal,
            token=token,
            crash_at="after_compound_state",
        )

    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, scope_ids FROM governance_session_grants"
        ).fetchone() == ("prepared", json.dumps([SCOPE_ID], separators=(",", ":")))
    recovered = reconcile_governance_operations(vault)
    assert recovered["activated"] == 1 and recovered["blocked"] is False

    connection = store.open_authorization_session_connection(vault)
    try:
        active, _identity = authorization_session_authority.active_session_grants(
            connection,
            context=context,
            audience=context.principal_id,
            purpose=None,
            path=path,
            fingerprint=fingerprint,
            scope_ids=(SCOPE_ID,),
            policy_fingerprint=connection.execute(
                "SELECT policy_fingerprint FROM active_governance_tuple WHERE singleton=1"
            ).fetchone()[0],
            now=V4_NOW + 3,
        )
        drifted, _identity = authorization_session_authority.active_session_grants(
            connection,
            context=context,
            audience=context.principal_id,
            purpose=None,
            path=path,
            fingerprint=fingerprint,
            scope_ids=(SCOPE_ID, "scope-added-after-review"),
            policy_fingerprint=connection.execute(
                "SELECT policy_fingerprint FROM active_governance_tuple WHERE singleton=1"
            ).fetchone()[0],
            now=V4_NOW + 3,
        )
    finally:
        connection.close()
    assert tuple(grant.scope_ids for grant in active) == ((SCOPE_ID,),)
    assert drifted == ()


def test_v4_grant_scope_tamper_blocks_receipt_recovery(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    principal, _context, token, _path, _fingerprint = _configure_v4_grant(
        vault, monkeypatch
    )
    with pytest.raises(GovernanceCrash, match="after_compound_state"):
        op_govern_memory(
            vault,
            operation="grant",
            principal=principal,
            token=token,
            crash_at="after_compound_state",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        connection.execute(
            "UPDATE governance_session_grants SET scope_ids=?",
            (json.dumps([SCOPE_ID, "unreviewed-scope"], separators=(",", ":")),),
        )
        connection.commit()

    recovered = reconcile_governance_operations(vault)

    assert recovered["activated"] == 0
    assert recovered["blocked"] is True


def test_grant_explicit_session_scope_activates_like_the_omitted_scope(vault: Path) -> None:
    from exomem.governance import egress, tokens
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=egress.LEVEL_EXCERPT,
        authorization_session="conversation-a",
    )

    granted = op_govern_memory(
        vault,
        operation="grant",
        scope="session",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )

    assert granted["status"] == "committed"
    assert granted["grant_id"]


def test_missing_or_foreign_authorization_session_refuses_session_changes(vault: Path) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _committed_policy(vault)
    for principal, handle in [(_external(None), None), (_external("a"), "b")]:
        with pytest.raises(GovernanceError) as err:
            op_govern_memory(
                vault,
                operation="declare",
                principal=principal,
                authorization_session=handle,
                purpose="audit",
            )
        assert err.value.code == "AUTHORIZATION_SESSION_REQUIRED"


def test_declare_and_revoke_are_self_session_scoped(vault: Path) -> None:
    from exomem.find_types import Hit
    from exomem.governance import egress, tokens
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    declared = op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="audit",
        duration_seconds=600,
    )
    assert declared["status"] == "committed"
    assert declared["direction"] == "narrowing"
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT purpose, status FROM governance_session_purpose WHERE authorization_session=?",
            ("conversation-a",),
        ).fetchone() == ("audit", "active")

    withheld = egress.annotate_hits(
        vault,
        [
            Hit(
                path="Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md",
                type="pattern",
                scope=None,
                title="restricted",
                updated="2026-01-01",
                excerpt="restricted excerpt",
            )
        ],
        principal=_external(),
        limit=1,
    )
    notice_claim = tokens.verify(
        vault,
        withheld.notices[0]["escalation_token"],
        audience="external",
        authorization_session="conversation-a",
        purpose="audit",
    )
    assert notice_claim.max_level == egress.RELEASE_FLOOR
    assert notice_claim.authorization_session == "conversation-a"
    assert notice_claim.purpose == "audit"

    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
        purpose="audit",
    )
    op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
        purpose="audit",
    )
    revoked = op_govern_memory(
        vault,
        operation="revoke",
        principal=_external(),
        authorization_session="conversation-a",
        scope="session",
    )
    assert revoked["revoked"] == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        statuses = {
            row[0]
            for row in conn.execute(
                "SELECT status FROM governance_session_grants WHERE authorization_session=?",
                ("conversation-a",),
            )
        }
    assert statuses == {"revoked"}
    changed = op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="incident-response",
        duration_seconds=600,
    )
    assert changed["direction"] == "narrowing"


def test_interrupted_purpose_replacement_keeps_active_purpose(vault: Path) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="audit",
    )
    with pytest.raises(GovernanceCrash, match="after_purpose_prepare"):
        op_govern_memory(
            vault,
            operation="declare",
            principal=_external(),
            authorization_session="conversation-a",
            purpose="incident-response",
            crash_at="after_purpose_prepare",
        )
    assert store.active_session_purpose(
        vault,
        audience="external",
        authorization_session="conversation-a",
    ) == "audit"
    assert reconcile_governance_operations(vault)["activated"] == 1
    assert store.active_session_purpose(
        vault,
        audience="external",
        authorization_session="conversation-a",
    ) == "incident-response"
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM governance_session_purpose_staging"
        ).fetchone()[0] == 0
    assert reconcile_governance_operations(vault)["activated"] == 0


@pytest.mark.parametrize(("column", "value"), [("principal_id", "tampered"), ("expires_at", 0)])
def test_prepared_purpose_authorization_field_tamper_blocks_recovery(
    vault: Path, column: str, value: object
) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(vault, operation="declare", principal=_external(),
                         authorization_session="conversation-a", purpose="audit",
                         crash_at="after_purpose_prepare")
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(f"UPDATE governance_session_purpose_staging SET {column}=?", (value,))
        conn.commit()
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_compound_grant_crashes_abort_prior_or_finish_exact_prepared(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    path = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    first_token = tokens.mint(
        vault,
        paths=[path],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=first_token,
            crash_at="after_child_intent:1",
        )
    prior = reconcile_governance_operations(vault)
    assert prior["aborted"] == 1 and not prior["blocked"]
    assert tokens.verify(
        vault,
        first_token,
        audience="external",
        authorization_session="conversation-a",
    )

    second_token = tokens.mint(
        vault,
        paths=[path],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=second_token,
            crash_at="after_compound_state",
        )
    prepared = reconcile_governance_operations(vault)
    assert prepared["activated"] == 1 and not prepared["blocked"]
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        token_status, grant_status = conn.execute(
            "SELECT t.status, g.status FROM withhold_tokens t "
            "JOIN governance_session_grants g ON g.token_jti=t.jti "
            "WHERE t.jti=?",
            (second_token.split(".")[1],),
        ).fetchone()
    assert (token_status, grant_status) == ("consumed", "active")


def test_prepared_token_or_grant_nonstatus_tamper_blocks_recovery(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_compound_state",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(
            "UPDATE withhold_tokens SET audience='tampered' WHERE jti=?",
            (token.split(".")[1],),
        )
        conn.commit()
    result = reconcile_governance_operations(vault)
    assert result["blocked"] is True and result["activated"] == 0


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("withhold_tokens", "authorization_session", "other-session"),
        ("withhold_tokens", "expires_at", 0),
        ("governance_session_grants", "audience", "tampered"),
        ("governance_session_grants", "ceiling", 0),
        ("governance_session_grants", "membership_manifest", "[]"),
        ("governance_session_grants", "policy_fingerprint", "tampered"),
    ],
)
def test_prepared_token_or_grant_authorization_field_tamper_blocks_recovery(
    vault: Path, table: str, column: str, value: object
) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external", max_level=5, authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(vault, operation="grant", principal=_external(),
                         authorization_session="conversation-a", token=token,
                         crash_at="after_compound_state")
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        if table == "withhold_tokens":
            conn.execute(f"UPDATE {table} SET {column}=? WHERE jti=?", (value, token.split(".")[1]))
        else:
            conn.execute(f"UPDATE {table} SET {column}=?", (value,))
        conn.commit()
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_pending_rows_are_ttl_pinned_then_retire_after_close(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    issued = 1_800_000_000
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
        now=issued,
        ttl_seconds=1,
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            now=issued,
            duration_seconds=1,
            crash_at="after_compound_state",
        )
    assert tokens.sweep(vault, now=issued + 10) == 0
    store.sweep_authoring_state(vault, now=issued + 10)
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status FROM governance_session_grants WHERE token_jti=?",
            (token.split(".")[1],),
        ).fetchone() == ("prepared",)
    assert reconcile_governance_operations(vault)["activated"] == 1
    assert tokens.sweep(vault, now=issued + 10) == 1
    assert store.sweep_authoring_state(vault, now=issued + 10) >= 1
    assert not reconcile_governance_operations(vault)["blocked"]


def test_loader_keeps_warm_last_good_and_blocks_cold_during_partial_yaml(
    vault: Path,
) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    _committed_policy(vault)
    prior = policy.load(vault)
    assert not prior.blocked and not prior.empty
    proposal = _propose(vault, ceiling=2)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_target_write:1",
        )
    warm = policy.load(vault)
    assert warm.fingerprint == prior.fingerprint
    assert any(finding["code"] == "governance_mutation_pending" for finding in warm.findings)
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    cold = policy.load(vault)
    assert cold.blocked


def test_exact_prepared_and_marker_removed_boundaries_reconcile_idempotently(
    vault: Path,
) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    proposal = _propose(vault, ceiling=2)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_marker_removal",
        )
    assert not (
        vault / "Knowledge Base" / "_Governance" / ".policy-mutation.pending.json"
    ).exists()
    pending = policy.load(vault)
    assert pending.fingerprint != policy._content_fingerprint(
        policy.governance_root(vault), policy._iter_policy_files(policy.governance_root(vault))
    )
    first = reconcile_governance_operations(vault)
    assert first["activated"] == 1 and not first["blocked"]
    second = reconcile_governance_operations(vault)
    assert second["activated"] == 0 and not second["blocked"]


def test_partial_third_state_and_final_without_terminal_block(vault: Path) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    documents = _proposal_documents(ceiling=2)
    documents["scopes/confidential-patterns.yaml"] = documents[
        "scopes/confidential-patterns.yaml"
    ].replace("Confidential patterns", "Restricted patterns")
    partial_proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Change both the governed scope and its release ceiling",
        documents=documents,
        selector_paths=[PATTERN_GLOB],
        target_ceiling=2,
        duration="standing",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=partial_proposal["proposal_id"],
            crash_at="after_target_write:1",
        )
    assert reconcile_governance_operations(vault)["blocked"]

    # A separate vault state is needed because partial state is deliberately
    # unrecoverable without manual repair.


def test_final_active_sidecar_without_terminal_is_not_accepted(vault: Path) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    proposal = _propose(vault, ceiling=2)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_prepare",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        event_id = conn.execute(
            "SELECT reserved_event_id FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE governance_proposals SET status='spent' WHERE proposal_id=?",
            (proposal["proposal_id"],),
        )
        conn.commit()
    assert event_id
    assert reconcile_governance_operations(vault)["blocked"]


def test_prepared_proposal_nonstatus_tamper_blocks_recovery(vault: Path) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_prepare",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(
            "UPDATE governance_proposals SET fingerprint_at_propose='tampered' WHERE proposal_id=?",
            (proposal["proposal_id"],),
        )
        conn.commit()
    result = reconcile_governance_operations(vault)
    assert result["blocked"] is True and result["activated"] == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("proposal_json", "{}"),
        ("fingerprint_at_propose", "tampered"),
        ("membership_manifest", "[]"),
        ("expires_at", 0),
        ("attempt_no", 999),
        ("attempt_nonce", "f" * 32),
        ("reserved_event_id", "0" * 64),
    ],
)
def test_prepared_proposal_authorization_field_tamper_blocks_recovery(
    vault: Path, column: str, value: object
) -> None:
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_prepare",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(
            f"UPDATE governance_proposals SET {column}=? WHERE proposal_id=?",
            (value, proposal["proposal_id"]),
        )
        conn.commit()
    result = reconcile_governance_operations(vault)
    assert result["blocked"] is True and result["activated"] == 0


def test_suspend_and_resume_toggle_rule_with_semantic_direction(vault: Path) -> None:
    from exomem.governance import decisions, policy, receipts
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    suspended = op_govern_memory(
        vault,
        operation="suspend",
        principal=owner_principal(),
        rule_ids=[RULE_ID],
    )
    current = policy.load(vault)
    rule = next(rule for rule in current.rules if rule.id == RULE_ID)
    assert rule.options["suspended"] is True
    assert decisions.decide([SCOPE_ID], audience="external", policy=current).level == 6
    assert suspended["direction"] == "widening"

    resumed = op_govern_memory(
        vault,
        operation="resume",
        principal=owner_principal(),
        rule_ids=[RULE_ID],
    )
    current = policy.load(vault)
    rule = next(rule for rule in current.rules if rule.id == RULE_ID)
    assert "suspended" not in rule.options
    assert decisions.decide([SCOPE_ID], audience="external", policy=current).level == 1
    assert resumed["direction"] == "narrowing"
    operations = {
        record["operation"]
        for record in receipts.event_records(vault)
        if record.get("event_type") == "critical" and record.get("phase") == "intent"
    }
    assert {"governance_rule_suspend", "governance_rule_resume"} <= operations


def test_undo_restores_archive_and_expires_stale_dependent_grant(vault: Path) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    target = vault / "Knowledge Base" / "Notes" / "Patterns" / "undo-target.md"
    target.write_text("# Changed after grant review\n", encoding="utf-8")
    second = _propose(vault, ceiling=2)
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=second["proposal_id"],
    )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session, audience, purpose, ceiling, paths, fingerprints, "
            "token_jti, status, prepared_event_id, created_at, expires_at) "
            "VALUES ('dependent', 'conversation-a', 'external', NULL, 5, ?, ?, "
            "'historical-token', 'active', NULL, 1, 9999999999)",
            ('["Knowledge Base/Notes/Patterns/undo-target.md"]', '["stale"]'),
        )
        conn.commit()

    result = op_govern_memory(
        vault,
        operation="undo",
        principal=owner_principal(),
    )
    assert result["status"] == "committed"
    assert result["direction"] == "narrowing"
    assert next(rule for rule in policy.load(vault).rules if rule.id == RULE_ID).ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        status = conn.execute(
            "SELECT status FROM governance_session_grants WHERE grant_id='dependent'"
        ).fetchone()[0]
    assert status == "expired"


def test_orphan_marker_blocks_without_creating_a_sidecar(vault: Path) -> None:
    from exomem.governance import policy

    marker = policy.governance_root(vault) / ".policy-mutation.pending.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"event_id":"orphan"}\n', encoding="utf-8")
    loaded = policy.load(vault)
    assert loaded.blocked
    assert not store.sidecar_path(vault).exists()


def _tag_policy_documents(*, tag: str = "confidential") -> dict[str, str]:
    return {
        "scopes/tagged.yaml": (
            "governance_version: 1\n"
            f"id: {SCOPE_ID}\n"
            f"tags: [\"{tag}\"]\n"
        ),
        "rules/tagged.yaml": (
            "governance_version: 1\n"
            f"id: {RULE_ID}\n"
            f"scope_ids: [\"{SCOPE_ID}\"]\n"
            "audience: external\n"
            "ceiling: 1\n"
        ),
    }


def _write_tagged_page(vault: Path, name: str, tag: str = "confidential") -> Path:
    target = vault / "Knowledge Base" / "Notes" / "Insights" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\ntags: [{tag}]\n---\n\n# Tagged page\n",
        encoding="utf-8",
    )
    return target


def test_propose_derives_exact_membership_from_prospective_tag_policy(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    target = _write_tagged_page(vault, "prospective-tag.md")
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Restrict confidential-tagged material",
        documents=_tag_policy_documents(),
        selector_paths=["caller/hint/that/matches/nothing/**"],
        target_ceiling=1,
        duration="standing",
    )
    assert proposal["membership_preview"]["count"] == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        manifest = conn.execute(
            "SELECT membership_manifest FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()[0]
    assert target.relative_to(vault).as_posix() in manifest
    assert "caller/hint" not in manifest


@pytest.mark.parametrize("mutation", ["add", "delete", "rename", "retag"])
def test_commit_refuses_exact_prospective_membership_drift(
    vault: Path, mutation: str
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    original = _write_tagged_page(vault, "membership-original.md")
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Restrict confidential-tagged material",
        documents=_tag_policy_documents(),
        selector_paths=[],
        target_ceiling=1,
        duration="standing",
    )
    if mutation == "add":
        _write_tagged_page(vault, "membership-added.md")
    elif mutation == "delete":
        original.unlink()
    elif mutation == "rename":
        original.rename(original.with_name("membership-renamed.md"))
    else:
        original.write_text("---\ntags: [public]\n---\n\n# Retagged\n", encoding="utf-8")
    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert error.value.code == "STALE_GOVERNANCE_POLICY"


@pytest.mark.parametrize(
    "documents",
    [
        {
            **_tag_policy_documents(),
            "rules/duplicate.yaml": _tag_policy_documents()["rules/tagged.yaml"],
        },
        {
            **_tag_policy_documents(),
            "rules/tagged.yaml": _tag_policy_documents()["rules/tagged.yaml"]
            + "unknown_enforcement_fact: true\n",
        },
    ],
)
def test_propose_refuses_invalid_complete_prospective_policy(
    vault: Path, documents: dict[str, str]
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Invalid policy must not become a proposal",
            documents=documents,
            selector_paths=[],
            target_ceiling=1,
        )
    assert error.value.code == "INVALID_GOVERNANCE_POLICY"
    if store.sidecar_path(vault).exists():
        with sqlite3.connect(store.sidecar_path(vault)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0] == 0


def test_existing_sidecar_orphan_marker_blocks_reconciliation(vault: Path) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import reconcile_governance_operations

    store.open_connection(vault).close()
    marker = policy.governance_root(vault) / ".policy-mutation.pending.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"event_id":"orphan"}\n', encoding="utf-8")
    assert reconcile_governance_operations(vault)["blocked"]


def test_legacy_sparse_open_component_blocks_recovery(vault: Path) -> None:
    from exomem.governance.tool import reconcile_governance_operations

    conn = store.open_connection(vault)
    try:
        conn.execute(
            "INSERT INTO governance_operation_journals "
            "(event_id, operation, causation_id, principal_id, phase, direction, prior_digest, "
            "prepared_digest, final_digest, affected_ids, required_child_intents, "
            "required_child_terminals, marker_required, created_at, updated_at) "
            "VALUES ('sparse', 'grant', 'sparse', 'external', 'pending', 'widening', "
            "'prior', 'prepared', 'final', '[]', '[]', '[]', 0, 0, 0)"
        )
        conn.execute(
            "INSERT INTO governance_operation_components "
            "(event_id, phase, ordinal, component_kind, component_key, value_json, value_hash, status) "
            "VALUES ('sparse', 'prepared', 0, 'token', 'missing', '{\"status\":\"prepared\"}', "
            "'legacy', 'prepared')"
        )
        conn.commit()
    finally:
        conn.close()
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_missing_marker_cannot_recover_prepared_yaml_without_terminal(vault: Path) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    proposal = _propose(vault, ceiling=2)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_prepare",
        )
    (policy.governance_root(vault) / ".policy-mutation.pending.json").unlink()
    assert reconcile_governance_operations(vault)["blocked"]


def test_mismatched_marker_blocks_reconciliation(vault: Path) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    proposal = _propose(vault, ceiling=2)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_marker",
        )
    marker = policy.governance_root(vault) / ".policy-mutation.pending.json"
    marker.write_text('{"event_id":"wrong"}\n', encoding="utf-8")
    assert reconcile_governance_operations(vault)["blocked"]


@pytest.mark.parametrize("mutation", ["extra_field", "symlink"])
def test_marker_requires_exact_regular_file_schema(vault: Path, mutation: str) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    proposal = _propose(vault, ceiling=2)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_marker",
        )
    marker = policy.governance_root(vault) / ".policy-mutation.pending.json"
    if mutation == "extra_field":
        payload = __import__("json").loads(marker.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        marker.write_text(__import__("json").dumps(payload), encoding="utf-8")
    else:
        target = marker.with_name("marker-target.json")
        marker.replace(target)
        marker.symlink_to(target.name)
    assert reconcile_governance_operations(vault)["blocked"]


def _purpose_policy_documents() -> dict[str, str]:
    documents = _proposal_documents(ceiling=1)
    documents["rules/confidential-patterns.yaml"] += "purpose: audit\n"
    return documents


def _commit_purpose_policy(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Restrict pattern references during audit sessions",
        documents=_purpose_policy_documents(),
        selector_paths=[PATTERN_GLOB],
        target_ceiling=1,
        duration="standing",
    )
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )


def test_declared_session_purpose_narrows_prompt_reference_gate(vault: Path) -> None:
    from exomem.governance import egress
    from exomem.governance.tool import op_govern_memory

    _commit_purpose_policy(vault)
    op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="audit",
    )
    rel = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    payload = {"prompt": f"Read [[{rel}]] before answering"}
    gated = egress.gate_artifact_references(
        vault,
        payload,
        principal=_external(),
    )
    assert gated == {"prompt": "Read [withheld] before answering"}


def test_declared_purpose_notice_grants_without_redundant_purpose(vault: Path) -> None:
    from exomem.find_types import Hit
    from exomem.governance import egress
    from exomem.governance.tool import op_govern_memory

    _commit_purpose_policy(vault)
    op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="audit",
    )
    rel = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    withheld = egress.annotate_hits(
        vault,
        [
            Hit(
                path=rel,
                type="pattern",
                scope=None,
                title="restricted",
                updated="2026-01-01",
                excerpt="restricted excerpt",
            )
        ],
        principal=_external(),
        limit=1,
    )
    granted = op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=withheld.notices[0]["escalation_token"],
    )
    assert granted["status"] == "committed"


def test_undo_expires_grant_when_restored_selector_membership_changes(
    vault: Path,
) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    documents = _proposal_documents(ceiling=1)
    documents["scopes/confidential-patterns.yaml"] = documents[
        "scopes/confidential-patterns.yaml"
    ].replace(PATTERN_GLOB, "Knowledge Base/Notes/Elsewhere/**")
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Move the confidential selector away from pattern notes",
        documents=documents,
        selector_paths=[PATTERN_GLOB],
        target_ceiling=1,
        duration="standing",
    )
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    rel = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    token = tokens.mint(
        vault,
        paths=[rel],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    granted = op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )
    op_govern_memory(vault, operation="undo", principal=owner_principal())
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        status = conn.execute(
            "SELECT status FROM governance_session_grants WHERE grant_id=?",
            (granted["grant_id"],),
        ).fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(governance_session_grants)")}
    assert status == "expired"
    assert {"membership_manifest", "policy_fingerprint"} <= columns


def test_prepared_dependent_grant_nonstatus_tamper_blocks_recovery(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external", max_level=5, authorization_session="conversation-a",
    )
    grant = op_govern_memory(
        vault, operation="grant", principal=_external(),
        authorization_session="conversation-a", token=token,
    )
    proposal = _propose(vault, ceiling=2)
    op_govern_memory(vault, operation="commit", principal=owner_principal(), proposal_id=proposal["proposal_id"])
    with pytest.raises(GovernanceCrash):
        op_govern_memory(vault, operation="undo", principal=owner_principal(), crash_at="after_prepare")
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute("UPDATE governance_session_grants SET audience='tampered' WHERE grant_id=?", (grant["grant_id"],))
        conn.commit()
    result = reconcile_governance_operations(vault)
    assert result["blocked"] is True and result["activated"] == 0


def test_explain_and_simulate_are_projected_effective_dry_runs(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    restricted = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    open_path = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"
    explained = op_govern_memory(
        vault,
        operation="explain",
        principal=_external(),
        audience="external",
        path=restricted,
    )
    assert explained["effective_ceiling"] == 1
    assert explained["rule_ids"] == [RULE_ID]
    assert explained["participating_chain"] == [RULE_ID]
    simulated = op_govern_memory(
        vault,
        operation="simulate",
        principal=_external(),
        audience="external",
        paths=[restricted, open_path],
    )
    assert simulated["evaluated_count"] == 2
    assert simulated["withheld_count"] == 1
    assert simulated["released_count"] == 1
    projected = str({"explain": explained, "simulate": simulated})
    assert "kill-switch" not in projected
    assert "restricted excerpt" not in projected
    assert "governance_version" not in projected


def test_inspection_requires_and_honours_explicit_audience(vault: Path) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _committed_policy(vault)
    restricted = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
    with pytest.raises(GovernanceError, match="INVALID_INSPECTION"):
        op_govern_memory(vault, operation="explain", principal=_external(), path=restricted)
    with pytest.raises(GovernanceError, match="UNSUPPORTED_INSPECTION_AUDIENCE"):
        op_govern_memory(
            vault,
            operation="explain",
            principal=_external(),
            audience="another-audience",
            path=restricted,
        )


@pytest.mark.parametrize(
    ("operation", "argument"),
    [
        ("explain", "path"),
        ("simulate", "paths"),
    ],
)
@pytest.mark.parametrize(
    "path_value",
    ["../outside.md", "/tmp/outside.md", "Knowledge Base/Notes/link.md"],
)
def test_inspection_refuses_noncanonical_or_symlinked_paths_without_an_oracle(
    vault: Path, operation: str, argument: str, path_value: str
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _committed_policy(vault)
    outside = vault.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = vault / "Knowledge Base" / "Notes" / "link.md"
    link.symlink_to(outside)
    value: object = path_value if argument == "path" else [path_value]

    with pytest.raises(GovernanceError) as raised:
        op_govern_memory(
            vault,
            operation=operation,
            principal=_external(),
            audience="external",
            **{argument: value},
        )

    assert raised.value.code == "INVALID_INSPECTION_PATH"


def test_revoke_recovery_preserves_the_terminal_revocation_timestamp(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    granted = op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )
    def crash_after_terminal(*_args, **_kwargs):
        raise GovernanceCrash("after_terminal")

    monkeypatch.setattr("exomem.governance.tool._activate_event", crash_after_terminal)
    with pytest.raises(GovernanceCrash, match="after_terminal"):
        op_govern_memory(
            vault,
            operation="revoke",
            scope="session",
            principal=_external(),
            authorization_session="conversation-a",
            now=123.0,
        )

    assert reconcile_governance_operations(vault)["activated"] == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status, revoked_at FROM governance_session_grants WHERE grant_id=?",
            (granted["grant_id"],),
        ).fetchone() == ("revoked", 123.0)


def test_commit_refuses_a_symlinked_policy_parent_before_receipt_or_write(vault: Path) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import GovernanceError, op_govern_memory

    proposal = _propose(vault)
    root = policy.governance_root(vault)
    outside = vault.parent / "outside-governance"
    outside.mkdir()
    root.mkdir(parents=True)
    (root / "rules").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GovernanceError, match="INVALID_GOVERNANCE_TARGET"):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )

    assert not list(outside.iterdir())
    assert receipts.event_records(vault) == []
    assert _journal_count(vault, "allocating") == 0


def test_allocating_journal_pins_expired_proposal_until_exact_prior_recovery(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import op_govern_memory, reconcile_governance_operations

    issued = 1_800_000_000
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Treat pattern notes as confidential for the external audience",
        documents=_proposal_documents(),
        ttl_seconds=1,
        now=issued,
    )

    def fail_intent(*_args, **_kwargs):
        raise receipts.ReceiptError("intent unavailable")

    monkeypatch.setattr(receipts, "begin_event", fail_intent)
    with pytest.raises(receipts.ReceiptError):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            now=issued,
        )

    assert store.sweep_authoring_state(vault, now=issued + 10) == 0
    recovery = reconcile_governance_operations(vault)
    assert recovery["blocked"] is False and recovery["aborted"] == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status, reserved_event_id FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone() == ("pending", None)


def test_owner_standing_grant_and_revoke_use_yaml_receipt_transition(vault: Path) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    standing_id = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
    granted = op_govern_memory(
        vault,
        operation="grant",
        scope="standing",
        principal=owner_principal(),
        grant_id=standing_id,
        scope_ids=[SCOPE_ID],
        audience="external",
        ceiling=5,
    )
    assert granted["status"] == "committed"
    assert any(grant.id == standing_id for grant in policy.load(vault).grants)
    revoked = op_govern_memory(
        vault,
        operation="revoke",
        scope="standing",
        principal=owner_principal(),
        grant_id=standing_id,
    )
    assert revoked["status"] == "committed"
    assert all(grant.id != standing_id for grant in policy.load(vault).grants)
    operations = {
        row["operation"]
        for row in receipts.event_records(vault)
        if row.get("phase") == "intent"
    }
    assert {"governance_standing_grant", "governance_standing_revoke"} <= operations


@pytest.mark.parametrize(
    "grant_id",
    [
        "../../../escaped-grant",
        "../../../../escaped-grant",
        "../escaped-grant",
        "nested/escaped-grant",
        r"nested\escaped-grant",
        "/tmp/escaped-grant",
        ".",
        "..",
        "%2e%2e%2fescaped-grant",
        "\u2024\u2024/escaped-grant",
        "nested\uff0fescaped-grant",
    ],
)
def test_standing_grant_rejects_noncanonical_id_without_path_escape(
    vault: Path, grant_id: str
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _committed_policy(vault)
    before_events = receipts.event_records(vault)
    sandbox_root = vault.parent
    before_files = {
        path.relative_to(sandbox_root).as_posix(): path.read_bytes()
        for path in sandbox_root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            vault,
            operation="grant",
            scope="standing",
            principal=owner_principal(),
            grant_id=grant_id,
            scope_ids=[SCOPE_ID],
            audience="external",
            ceiling=5,
        )
    assert error.value.code == "INVALID_STANDING_GRANT_ID"
    after_files = {
        path.relative_to(sandbox_root).as_posix(): path.read_bytes()
        for path in sandbox_root.rglob("*")
        if path.is_file()
    }
    assert after_files == before_files
    assert receipts.event_records(vault) == before_events
    assert list((policy.governance_root(vault) / "grants").glob("*.yaml")) == []


def test_standing_grant_valid_id_is_one_direct_grants_child(vault: Path) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    grant_id = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
    op_govern_memory(
        vault,
        operation="grant",
        scope="standing",
        principal=owner_principal(),
        grant_id=grant_id,
        scope_ids=[SCOPE_ID],
        audience="external",
        ceiling=5,
    )
    grants_root = policy.governance_root(vault) / "grants"
    assert list(grants_root.glob("*.yaml")) == [grants_root / f"{grant_id}.yaml"]


def _journal_count(vault: Path, phase: str = "pending") -> int:
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM governance_operation_journals WHERE phase=?",
            (phase,),
        ).fetchone()[0]


def test_commit_receipt_failure_keeps_only_allocating_control_row(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import op_govern_memory, reconcile_governance_operations

    proposal = _propose(vault)

    def fail_intent(*_args, **_kwargs):
        raise receipts.ReceiptError("intent unavailable")

    monkeypatch.setattr(receipts, "begin_event", fail_intent)
    with pytest.raises(receipts.ReceiptError):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert _journal_count(vault) == 0
    assert _journal_count(vault, "allocating") == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()[0] == "pending"
    assert reconcile_governance_operations(vault)["aborted"] == 1


@pytest.mark.parametrize(
    ("crash_at", "intent_expected"),
    [("after_reservation", False), ("after_intent_before_journal", True)],
)
def test_orphan_commit_reservation_recovers_exact_prior_and_retries_with_new_identity(
    vault: Path, crash_at: str, intent_expected: bool
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at=crash_at,
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        reserved = conn.execute(
            "SELECT attempt_no, attempt_nonce, reserved_event_id "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()
    assert reserved is not None
    orphan_event_id = str(reserved[2])
    assert reserved[0] == 1 and reserved[1] and orphan_event_id
    assert _journal_count(vault) == 0
    assert _journal_count(vault, "allocating") == 1
    assert not (policy.governance_root(vault) / ".policy-mutation.pending.json").exists()
    assert not (policy.governance_root(vault) / "scopes").exists()
    phases = [
        row["phase"]
        for row in receipts.event_records(vault)
        if row.get("event_id") == orphan_event_id
        or row.get("causation_id") == orphan_event_id
    ]
    assert ("intent" in phases) is intent_expected

    recovery = reconcile_governance_operations(vault)
    assert recovery["blocked"] is False
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status, attempt_nonce, reserved_event_id "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone() == ("pending", None, None)
    phases = [
        row["phase"]
        for row in receipts.event_records(vault)
        if row.get("event_id") == orphan_event_id
        or row.get("causation_id") == orphan_event_id
    ]
    assert ("aborted" in phases) is intent_expected
    assert _journal_count(vault, "allocating") == 0
    assert not (policy.governance_root(vault) / ".policy-mutation.pending.json").exists()
    assert not (policy.governance_root(vault) / "scopes").exists()

    committed = op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    assert committed["event_id"] != orphan_event_id
    assert committed["status"] == "committed"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("attempt_no", "not-a-number", id="nonnumeric-attempt"),
        pytest.param("attempt_no", 0, id="zero-attempt"),
        pytest.param("attempt_no", -1, id="negative-attempt"),
        pytest.param(
            "attempt_no", "9223372036854775808", id="overflow-attempt"
        ),
        pytest.param("attempt_nonce", None, id="missing-nonce"),
        pytest.param("attempt_nonce", "", id="empty-nonce"),
        pytest.param("attempt_nonce", "abcd", id="short-nonce"),
        pytest.param("attempt_nonce", "G" * 32, id="invalid-nonce"),
        pytest.param("reserved_event_id", None, id="missing-event"),
        pytest.param("reserved_event_id", "", id="empty-event"),
        pytest.param("reserved_event_id", "not-an-event", id="malformed-event"),
        pytest.param("reserved_event_id", "f" * 64, id="mismatched-event"),
    ],
)
def test_malformed_orphan_reservation_identity_blocks_without_mutation_or_receipt(
    vault: Path, column: str, value: object
) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_reservation",
        )
    updates = {
        "attempt_no": "UPDATE governance_proposals SET attempt_no=? WHERE proposal_id=?",
        "attempt_nonce": (
            "UPDATE governance_proposals SET attempt_nonce=? WHERE proposal_id=?"
        ),
        "reserved_event_id": (
            "UPDATE governance_proposals SET reserved_event_id=? WHERE proposal_id=?"
        ),
    }
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(updates[column], (value, proposal["proposal_id"]))
        conn.commit()
        before = conn.execute(
            "SELECT typeof(attempt_no), quote(attempt_no), typeof(attempt_nonce), "
            "quote(attempt_nonce), typeof(reserved_event_id), quote(reserved_event_id) "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()
    before_receipts = receipts.event_records(vault)

    result = reconcile_governance_operations(vault)

    assert result["blocked"] is True
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        after = conn.execute(
            "SELECT typeof(attempt_no), quote(attempt_no), typeof(attempt_nonce), "
            "quote(attempt_nonce), typeof(reserved_event_id), quote(reserved_event_id) "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()
    assert after == before
    assert receipts.event_records(vault) == before_receipts


def test_commit_revalidates_exact_membership_before_persisting_reservation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceError, op_govern_memory

    proposal = _propose(vault)
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Patterns"
        / "kill-switch-for-risky-releases.md"
    )
    original = governance_tool._validate_proposal_values
    calls = 0

    def mutate_at_final_validation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            target.write_text("# Changed at final validation\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        governance_tool, "_validate_proposal_values", mutate_at_final_validation
    )
    with pytest.raises(GovernanceError) as err:
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert err.value.code == "STALE_GOVERNANCE_POLICY"
    assert calls >= 3
    assert _journal_count(vault) == 0
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone() == ("pending", None)


def test_final_stale_reservation_check_precedes_durable_intent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceError, op_govern_memory

    proposal = _propose(vault)
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Patterns"
        / "kill-switch-for-risky-releases.md"
    )
    original = governance_tool._validate_proposal_values
    calls = 0

    def drift_at_final_check(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            target.write_text("# Drift before final reservation check\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        governance_tool, "_validate_proposal_values", drift_at_final_check
    )
    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert error.value.code == "STALE_GOVERNANCE_POLICY"
    assert calls >= 3
    assert receipts.event_records(vault) == []
    assert _journal_count(vault) == 0
    assert not (policy.governance_root(vault) / ".policy-mutation.pending.json").exists()
    assert not (policy.governance_root(vault) / "scopes").exists()
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone() == ("pending", None)
        assert conn.execute("SELECT COUNT(*) FROM governance_policy_archives").fetchone() == (
            0,
        )


def test_grant_receipt_failure_leaves_allocating_control_row_and_prior_state(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import receipts, tokens
    from exomem.governance.tool import op_govern_memory, reconcile_governance_operations

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )

    def fail_intent(*_args, **_kwargs):
        raise receipts.ReceiptError("intent unavailable")

    monkeypatch.setattr(receipts, "begin_event", fail_intent)
    with pytest.raises(receipts.ReceiptError):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
        )
    assert _journal_count(vault) == 0
    assert _journal_count(vault, "allocating") == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT consumed_at, prepared_event_id FROM withhold_tokens WHERE jti=?",
            (token.split(".")[1],),
        ).fetchone() == (None, None)
        assert conn.execute("SELECT COUNT(*) FROM governance_session_grants").fetchone()[0] == 0
    assert reconcile_governance_operations(vault)["aborted"] == 1


def test_revoke_receipt_failure_leaves_allocating_control_row_and_prior_state(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import receipts, tokens
    from exomem.governance.tool import op_govern_memory, reconcile_governance_operations

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    grant = op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )

    def fail_intent(*_args, **_kwargs):
        raise receipts.ReceiptError("intent unavailable")

    monkeypatch.setattr(receipts, "begin_event", fail_intent)
    with pytest.raises(receipts.ReceiptError):
        op_govern_memory(
            vault,
            operation="revoke",
            principal=_external(),
            authorization_session="conversation-a",
            scope="session",
        )
    assert _journal_count(vault) == 0
    assert _journal_count(vault, "allocating") == 1
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT status FROM governance_session_grants WHERE grant_id=?",
            (grant["grant_id"],),
        ).fetchone() == ("active",)
    assert reconcile_governance_operations(vault)["aborted"] == 1


@pytest.mark.parametrize("operation", ["declare", "suspend"])
def test_single_transition_receipt_failure_leaves_allocating_control_row_and_prior_target(
    vault: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import op_govern_memory, reconcile_governance_operations

    _committed_policy(vault)
    rule_path = policy.governance_root(vault) / "rules/confidential-patterns.yaml"
    prior_rule = rule_path.read_bytes()

    def fail_intent(*_args, **_kwargs):
        raise receipts.ReceiptError("intent unavailable")

    monkeypatch.setattr(receipts, "begin_event", fail_intent)
    kwargs = (
        {
            "operation": "declare",
            "principal": _external(),
            "authorization_session": "conversation-a",
            "purpose": "audit",
        }
        if operation == "declare"
        else {
            "operation": "suspend",
            "principal": owner_principal(),
            "rule_ids": [RULE_ID],
        }
    )
    with pytest.raises(receipts.ReceiptError):
        op_govern_memory(vault, **kwargs)
    assert _journal_count(vault) == 0
    assert _journal_count(vault, "allocating") == 1
    assert rule_path.read_bytes() == prior_rule
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_session_purpose").fetchone()[0] == 0
    assert reconcile_governance_operations(vault)["aborted"] == 1


def test_authorization_projection_rejects_reserved_version_field() -> None:
    from exomem.governance.transaction import GovernanceError, authorization_row

    with pytest.raises(GovernanceError) as error:
        authorization_row(projection_version=2, status="active")
    assert error.value.code == "INVALID_GOVERNANCE_PROJECTION"


def _write_shadow_rule(vault: Path) -> None:
    from exomem.governance import policy

    target = policy.governance_root(vault) / "rules/shadow.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB1\n"
        f'scope_ids: ["{SCOPE_ID}"]\n'
        "audience: external\n"
        "ceiling: 1\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()


def test_effective_direction_treats_shadowed_suspend_as_narrowing(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    _write_shadow_rule(vault)
    result = op_govern_memory(
        vault, operation="suspend", principal=owner_principal(), rule_ids=[RULE_ID]
    )
    assert result["direction"] == "narrowing"


def test_effective_commit_and_undo_match_shadowed_proposal_direction(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    _write_shadow_rule(vault)
    widened = _propose(vault, ceiling=5)
    assert widened["consequences"]["direction"] == "narrowing"
    committed = op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=widened["proposal_id"],
    )
    assert committed["direction"] == widened["consequences"]["direction"]

    narrowed = _propose(vault, ceiling=1)
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=narrowed["proposal_id"],
    )
    undone = op_govern_memory(vault, operation="undo", principal=owner_principal())
    assert undone["direction"] == "narrowing"


def test_effective_resume_membership_failure_is_conservatively_widening(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _committed_policy(vault)
    op_govern_memory(vault, operation="suspend", principal=owner_principal(), rule_ids=[RULE_ID])

    def unresolved(*_args, **_kwargs):
        raise GovernanceError("MEMBERSHIP_UNRESOLVED", "forced for direction proof")

    monkeypatch.setattr(governance_tool, "_memberships_for_path", unresolved)
    result = op_govern_memory(
        vault, operation="resume", principal=owner_principal(), rule_ids=[RULE_ID]
    )
    assert result["direction"] == "widening"


def test_proposal_hints_only_add_diagnostics_not_policy_facts(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    baseline = _propose(vault)
    hinted = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Treat pattern notes as confidential for the external audience",
        documents=_proposal_documents(),
        selector_paths=["caller/does-not-control-membership/**"],
        target_ceiling=5,
        duration="standing",
    )
    assert hinted["hint_diagnostics"] == [
        "selector_paths are compatibility hints; concrete membership is authoritative",
        "target_ceiling is a compatibility hint, not an authorization fact",
    ]
    for field in ("membership_preview", "consequences", "overlaps"):
        assert hinted[field] == baseline[field]


@pytest.mark.parametrize("mutation", ["noncanonical", "hash"])
def test_recovery_validates_every_stored_component_before_classification(
    vault: Path, mutation: str
) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_compound_state",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute("DROP TRIGGER governance_components_no_update")
        event_id = conn.execute(
            "SELECT event_id FROM governance_operation_journals WHERE phase='pending'"
        ).fetchone()[0]
        if mutation == "noncanonical":
            conn.execute(
                "UPDATE governance_operation_components SET value_json=value_json || ' ' "
                "WHERE event_id=? AND phase='final' AND ordinal=0",
                (event_id,),
            )
        else:
            conn.execute(
                "UPDATE governance_operation_components SET value_hash='0' "
                "WHERE event_id=? AND phase='final' AND ordinal=0",
                (event_id,),
            )
        conn.commit()
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_recovery_requires_receipt_intent_to_match_journal_binding(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_compound_state",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute("DROP TRIGGER governance_components_no_update")
        conn.execute(
            "UPDATE governance_operation_journals SET affected_ids='[\"not-the-receipt\"]' "
            "WHERE phase='pending'"
        )
        conn.commit()
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_recovery_checks_noncurrent_final_components_before_activation(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_compound_state",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute("DROP TRIGGER governance_components_no_update")
        conn.execute(
            "UPDATE governance_operation_components SET value_hash='0' "
            "WHERE phase='final' AND ordinal=0"
        )
        conn.commit()
    assert reconcile_governance_operations(vault)["blocked"] is True


@pytest.mark.parametrize("boundary", ["after_child_intent:1", "after_child_intent:2"])
def test_compound_allocating_recovery_aborts_only_observed_child_intents(
    vault: Path, boundary: str
) -> None:
    from exomem.governance import receipts, tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at=boundary,
        )
    before = receipts.event_records(vault)
    observed = [
        record["event_id"]
        for record in before
        if record.get("phase") == "intent" and record.get("parent_causation_id")
    ]
    assert len(observed) == int(boundary.rsplit(":", 1)[1])
    assert reconcile_governance_operations(vault)["aborted"] == 1
    terminals = [
        record["causation_id"]
        for record in receipts.event_records(vault)
        if record.get("phase") == "aborted" and record.get("causation_id") in observed
    ]
    assert set(terminals) == set(observed)
    assert tokens.verify(
        vault, token, audience="external", authorization_session="conversation-a"
    )


@pytest.mark.parametrize("boundary", ["after_child_terminal:1", "after_child_terminal:2"])
def test_compound_prepared_recovery_fills_only_missing_terminals_once(
    vault: Path, boundary: str
) -> None:
    from exomem.governance import receipts, tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at=boundary,
        )
    assert reconcile_governance_operations(vault)["activated"] == 1
    records = receipts.event_records(vault)
    intents = [
        record["event_id"]
        for record in records
        if record.get("phase") == "intent" and record.get("parent_causation_id")
    ]
    committed = [
        record["causation_id"]
        for record in records
        if record.get("phase") == "committed" and record.get("causation_id") in intents
    ]
    assert sorted(committed) == sorted(intents)
    assert len(committed) == len(set(committed)) == 2
    assert reconcile_governance_operations(vault)["activated"] == 0
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT t.status, g.status FROM withhold_tokens t "
            "JOIN governance_session_grants g ON g.token_jti=t.jti WHERE t.jti=?",
            (token.split(".")[1],),
        ).fetchone() == ("consumed", "active")


def test_compound_exact_prior_with_committed_child_blocks_without_abort(vault: Path) -> None:
    from exomem.governance import receipts, tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash, match="after_child_terminal:1"):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_child_terminal:1",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        event_id = conn.execute(
            "SELECT event_id FROM governance_operation_journals WHERE phase='pending'"
        ).fetchone()[0]
        token_key, token_prior = conn.execute(
            "SELECT component_key, value_json FROM governance_operation_components "
            "WHERE event_id=? AND phase='prior' AND component_kind='token'",
            (event_id,),
        ).fetchone()
        grant_key = conn.execute(
            "SELECT component_key FROM governance_operation_components "
            "WHERE event_id=? AND phase='prior' AND component_kind='grant'",
            (event_id,),
        ).fetchone()[0]
        prior = json.loads(token_prior)
        conn.execute(
            "UPDATE withhold_tokens SET consumed_at=?, status=?, prepared_event_id=? WHERE jti=?",
            (prior["consumed_at"], prior["status"], prior["prepared_event_id"], token_key),
        )
        conn.execute("DELETE FROM governance_session_grants WHERE grant_id=?", (grant_key,))
        conn.commit()
    before_records = receipts.event_records(vault)
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        before_grants = conn.execute(
            "SELECT COUNT(*) FROM governance_session_grants"
        ).fetchone()[0]
    assert tokens.verify(
        vault, token, audience="external", authorization_session="conversation-a"
    )

    recovered = reconcile_governance_operations(vault)
    assert recovered["blocked"] is True and recovered["aborted"] == 0
    assert receipts.event_records(vault) == before_records
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_session_grants").fetchone()[0] == (
            before_grants
        )
    assert tokens.verify(
        vault, token, audience="external", authorization_session="conversation-a"
    )


def test_compound_activation_rolls_back_before_retrying_exact_prepared(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import recovery as recovery_module
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_compound_state",
        )
    real = recovery_module._RECOVERY_ACTIVATORS["compound_grant"]

    def mutate_then_raise(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("activation crash")

    monkeypatch.setattr(
        recovery_module,
        "_RECOVERY_ACTIVATORS",
        {**recovery_module._RECOVERY_ACTIVATORS, "compound_grant": mutate_then_raise},
    )
    with pytest.raises(RuntimeError, match="activation crash"):
        reconcile_governance_operations(vault)
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT t.status, g.status, j.phase FROM withhold_tokens t "
            "JOIN governance_session_grants g ON g.token_jti=t.jti "
            "JOIN governance_operation_journals j ON j.event_id=t.prepared_event_id "
            "WHERE t.jti=?",
            (token.split(".")[1],),
        ).fetchone() == ("prepared", "prepared", "pending")
    monkeypatch.setattr(
        recovery_module,
        "_RECOVERY_ACTIVATORS",
        {**recovery_module._RECOVERY_ACTIVATORS, "compound_grant": real},
    )
    assert reconcile_governance_operations(vault)["activated"] == 1


def test_final_proposal_encoding_without_terminal_blocks_recovery(vault: Path) -> None:
    from exomem.governance import recovery as recovery_module
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_prepare",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        event_id = conn.execute(
            "SELECT event_id FROM governance_operation_journals WHERE phase='pending'"
        ).fetchone()[0]
        final = __import__("json").loads(
            conn.execute(
                "SELECT value_json FROM governance_operation_components "
                "WHERE event_id=? AND phase='final' AND component_kind='proposal'",
                (event_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE governance_proposals SET proposal_json=?, fingerprint_at_propose=?, "
            "membership_manifest=?, status=?, expires_at=?, attempt_no=?, attempt_nonce=?, "
            "reserved_event_id=?, created_at=?, spent_at=? WHERE proposal_id=?",
            (
                final["proposal_json"],
                final["fingerprint_at_propose"],
                final["membership_manifest"],
                final["status"],
                final["expires_at"],
                final["attempt_no"],
                final["attempt_nonce"],
                final["reserved_event_id"],
                final["created_at"],
                final["spent_at"],
                proposal["proposal_id"],
            ),
        )
        conn.commit()
        assert recovery_module._matches_phase(vault, conn, event_id, "final")
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_after_intent_is_armed_pending_no_marker_exact_prior(vault: Path) -> None:
    from exomem.governance import policy
    from exomem.governance import recovery as recovery_module
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="suspend",
            principal=owner_principal(),
            rule_ids=[RULE_ID],
            crash_at="after_intent",
        )
    marker = policy.governance_root(vault) / ".policy-mutation.pending.json"
    assert not marker.exists()
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        event_id, phase = conn.execute(
            "SELECT event_id, phase FROM governance_operation_journals "
            "WHERE phase='pending'"
        ).fetchone()
        assert phase == "pending"
        assert recovery_module._matches_phase(vault, conn, event_id, "prior")
    assert reconcile_governance_operations(vault)["aborted"] == 1


def test_commit_post_intent_membership_drift_aborts_exact_prior_and_retries(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import GovernanceError, op_govern_memory

    proposal = _propose(vault)
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Patterns"
        / "kill-switch-for-risky-releases.md"
    )
    original = target.read_bytes()
    real_begin = receipts.begin_event

    def begin_then_drift(*args, **kwargs):
        real_begin(*args, **kwargs)
        target.write_text("# Drifted after durable intent\n", encoding="utf-8")

    monkeypatch.setattr(receipts, "begin_event", begin_then_drift)
    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
        )
    assert error.value.code == "STALE_GOVERNANCE_POLICY"
    assert not (policy.governance_root(vault) / ".policy-mutation.pending.json").exists()
    assert not (policy.governance_root(vault) / "scopes").exists()
    records = receipts.event_records(vault)
    event_id = next(record["event_id"] for record in records if record["phase"] == "intent")
    assert any(
        record.get("causation_id") == event_id
        and record.get("phase") == "aborted"
        for record in records
    )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        assert conn.execute(
            "SELECT phase FROM governance_operation_journals WHERE event_id=?", (event_id,)
        ).fetchone() == ("aborted",)
        assert conn.execute(
            "SELECT status, reserved_event_id FROM governance_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone() == ("pending", None)

    monkeypatch.setattr(receipts, "begin_event", real_begin)
    target.write_bytes(original)
    retry = op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    assert retry["event_id"] != event_id


@pytest.mark.parametrize("drift", ["edit", "new_member"])
def test_terminal_commit_membership_guard_blocks_until_exact_corpus_restoration(
    vault: Path, drift: str
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    proposal = _propose(vault)
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Patterns"
        / "kill-switch-for-risky-releases.md"
    )
    original = target.read_bytes()
    with pytest.raises(GovernanceCrash, match="after_terminal"):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_terminal",
        )
    records_before = receipts.event_records(vault)
    terminal_count = sum(record["phase"] == "committed" for record in records_before)
    rule_path = policy.governance_root(vault) / "rules" / "confidential-patterns.yaml"
    rule_before = rule_path.read_bytes()
    added = target.with_name("new-member-after-terminal.md")
    if drift == "edit":
        target.write_text("# Edited after terminal\n", encoding="utf-8")
    else:
        added.write_text("# New matching member\n", encoding="utf-8")

    assert reconcile_governance_operations(vault)["blocked"] is True
    assert rule_path.read_bytes() == rule_before
    if drift == "edit":
        target.write_bytes(original)
    else:
        added.unlink()
    assert reconcile_governance_operations(vault)["activated"] == 1
    assert rule_path.read_bytes() == rule_before
    records_after = receipts.event_records(vault)
    assert sum(record["phase"] == "committed" for record in records_after) == terminal_count


def test_identical_suspend_after_intent_recovers_exact_prior_without_marker(vault: Path) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    first = op_govern_memory(
        vault, operation="suspend", principal=owner_principal(), rule_ids=[RULE_ID]
    )
    rule_path = policy.governance_root(vault) / "rules" / "confidential-patterns.yaml"
    unchanged = rule_path.read_bytes()
    with pytest.raises(GovernanceCrash, match="after_intent"):
        op_govern_memory(
            vault,
            operation="suspend",
            principal=owner_principal(),
            rule_ids=[RULE_ID],
            crash_at="after_intent",
        )
    marker = policy.governance_root(vault) / ".policy-mutation.pending.json"
    assert not marker.exists()
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        event_id = conn.execute(
            "SELECT event_id FROM governance_operation_journals WHERE phase='pending'"
        ).fetchone()[0]
        yaml_images = conn.execute(
            "SELECT phase, value_json FROM governance_operation_components "
            "WHERE event_id=? AND component_kind='yaml' AND phase IN ('prior', 'prepared') "
            "ORDER BY phase",
            (event_id,),
        ).fetchall()
    assert yaml_images[0][1] == yaml_images[1][1]
    assert [record["phase"] for record in receipts.event_records(vault) if record["event_id"] == event_id] == [
        "intent"
    ]

    recovery = reconcile_governance_operations(vault)
    assert recovery["aborted"] == 1 and recovery["blocked"] is False
    assert rule_path.read_bytes() == unchanged
    assert any(
        record.get("causation_id") == event_id
        and record.get("phase") == "aborted"
        for record in receipts.event_records(vault)
    )
    retry = op_govern_memory(
        vault, operation="suspend", principal=owner_principal(), rule_ids=[RULE_ID]
    )
    assert retry["event_id"] not in {first["event_id"], event_id}


def test_identical_commit_after_terminal_guard_drift_blocks_then_activates(
    vault: Path,
) -> None:
    from exomem.governance import policy, receipts
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    governance_root = policy.governance_root(vault)
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Repeat the committed policy exactly",
        documents={
            rel: (governance_root / rel).read_text(encoding="utf-8")
            for rel in _proposal_documents()
        },
        selector_paths=[PATTERN_GLOB],
        target_ceiling=1,
        duration="standing",
    )
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Patterns"
        / "kill-switch-for-risky-releases.md"
    )
    original = target.read_bytes()
    rule_path = governance_root / "rules" / "confidential-patterns.yaml"
    unchanged_yaml = rule_path.read_bytes()
    with pytest.raises(GovernanceCrash, match="after_terminal"):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_terminal",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        event_id = conn.execute(
            "SELECT event_id FROM governance_operation_journals WHERE phase='pending'"
        ).fetchone()[0]
        yaml_images = conn.execute(
            "SELECT phase, component_key, value_json FROM governance_operation_components "
            "WHERE event_id=? AND component_kind='yaml' AND phase IN ('prior', 'prepared') "
            "ORDER BY phase, component_key",
            (event_id,),
        ).fetchall()
    prior_yaml = {
        key: value for phase, key, value in yaml_images if phase == "prior"
    }
    prepared_yaml = {
        key: value for phase, key, value in yaml_images if phase == "prepared"
    }
    assert prior_yaml == prepared_yaml
    terminal_count = sum(
        record["phase"] == "committed" for record in receipts.event_records(vault)
    )
    target.write_text("# Drifted after committed terminal\n", encoding="utf-8")

    assert reconcile_governance_operations(vault)["blocked"] is True
    assert rule_path.read_bytes() == unchanged_yaml
    target.write_bytes(original)
    assert reconcile_governance_operations(vault)["activated"] == 1
    assert rule_path.read_bytes() == unchanged_yaml
    assert sum(
        record["phase"] == "committed" for record in receipts.event_records(vault)
    ) == terminal_count


def test_final_guard_comparison_is_the_commit_linearization_point(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import policy
    from exomem.governance import recovery as recovery_module
    from exomem.governance.tool import op_govern_memory, reconcile_governance_operations

    proposal = _propose(vault)
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Patterns"
        / "kill-switch-for-risky-releases.md"
    )
    real_matches = recovery_module._matches_phase
    changed = False

    def mutate_after_final(*args, **kwargs):
        nonlocal changed
        matched = real_matches(*args, **kwargs)
        if args[3] == "final" and matched and not changed:
            changed = True
            target.write_text("# Changed after final guard comparison\n", encoding="utf-8")
        return matched

    monkeypatch.setattr(recovery_module, "_matches_phase", mutate_after_final)
    committed = op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    assert committed["status"] == "committed" and changed
    assert not policy.load(vault).blocked
    assert reconcile_governance_operations(vault)["blocked"] is False


def test_undo_recovery_never_replays_membership_resolution(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import tokens
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )
    proposal = _propose(vault, ceiling=2)
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="undo",
            principal=owner_principal(),
            crash_at="after_prepare",
        )
    calls = 0

    def replayed_membership(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("reconciliation must not replay semantic membership resolution")

    monkeypatch.setattr(governance_tool, "_resolved_membership_manifest", replayed_membership)
    assert reconcile_governance_operations(vault)["activated"] == 1
    assert reconcile_governance_operations(vault)["activated"] == 0
    assert calls == 0


def test_persisted_authorization_components_cover_exact_recovery_projections(
    vault: Path,
) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )
    op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="audit",
    )
    proposal = _propose(vault, ceiling=2)
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )
    op_govern_memory(vault, operation="undo", principal=owner_principal())
    projections = {
        "proposal": {
            "proposal_json", "fingerprint_at_propose", "membership_manifest", "status",
            "expires_at", "attempt_no", "attempt_nonce", "reserved_event_id", "created_at",
            "spent_at",
        },
        "token": {
            "audience", "max_level", "fingerprints", "paths", "expires_at", "minted_at",
            "consumed_at", "authorization_session", "purpose", "org_ceiling", "status",
            "prepared_event_id",
        },
        "grant": {
            "authorization_session", "audience", "purpose", "ceiling", "paths", "fingerprints",
            "token_jti", "status", "prepared_event_id", "created_at", "expires_at", "revoked_at",
            "membership_manifest", "policy_fingerprint",
        },
        "dependent_grant": {
            "authorization_session", "audience", "purpose", "ceiling", "paths", "fingerprints",
            "token_jti", "status", "prepared_event_id", "created_at", "expires_at", "revoked_at",
            "membership_manifest", "policy_fingerprint",
        },
        "purpose": {
            "authorization_session", "principal_id", "purpose", "status", "prepared_event_id",
            "created_at", "expires_at",
        },
    }
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        for kind, fields in projections.items():
            values = [
                __import__("json").loads(row[0])
                for row in conn.execute(
                    "SELECT value_json FROM governance_operation_components "
                    "WHERE component_kind=? ORDER BY event_id, phase, ordinal",
                    (kind,),
                )
            ]
            non_absent = [value for value in values if value.get("status") != "absent"]
            assert non_absent
            assert all(set(value) == {"projection_version", *fields} for value in non_absent)


def test_undo_direction_can_be_widening(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    first = _propose(vault, ceiling=5)
    op_govern_memory(
        vault, operation="commit", principal=owner_principal(), proposal_id=first["proposal_id"]
    )
    second = _propose(vault, ceiling=1)
    op_govern_memory(
        vault, operation="commit", principal=owner_principal(), proposal_id=second["proposal_id"]
    )
    assert op_govern_memory(vault, operation="undo", principal=owner_principal())["direction"] == "widening"


def test_declare_direction_can_narrow_or_conservatively_widen(vault: Path) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import op_govern_memory

    _commit_purpose_policy(vault)
    narrowed = op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="audit",
    )
    assert narrowed["direction"] == "narrowing"

    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
        purpose="audit",
    )
    op_govern_memory(
        vault,
        operation="grant",
        principal=_external(),
        authorization_session="conversation-a",
        token=token,
    )
    widened = op_govern_memory(
        vault,
        operation="declare",
        principal=_external(),
        authorization_session="conversation-a",
        purpose="incident-response",
    )
    assert widened["direction"] == "widening"


def test_recovery_rejects_terminal_list_that_does_not_match_required_children(
    vault: Path,
) -> None:
    from exomem.governance import tokens
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    _committed_policy(vault)
    token = tokens.mint(
        vault,
        paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
        audience="external",
        max_level=5,
        authorization_session="conversation-a",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
            crash_at="after_compound_state",
        )
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        conn.execute(
            "UPDATE governance_operation_journals SET required_child_terminals='[]' "
            "WHERE phase='pending'"
        )
        conn.commit()
    assert reconcile_governance_operations(vault)["blocked"] is True


def test_empty_complete_membership_proposal_is_private_and_narrowing(vault: Path) -> None:
    from exomem.governance.tool import op_govern_memory

    documents = _proposal_documents()
    documents["scopes/confidential-patterns.yaml"] = (
        "governance_version: 1\n"
        f"id: {SCOPE_ID}\n"
        "name: Empty scope\n"
        'paths: ["Knowledge Base/Does-Not-Exist/**"]\n'
    )
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Prepare an empty governed scope",
        documents=documents,
        selector_paths=[],
        target_ceiling=1,
    )
    assert proposal["membership_preview"] == {"count": 0, "samples": []}
    assert proposal["consequences"]["direction"] == "narrowing"
    assert proposal["consequences"]["target_ceiling"] is None
    assert proposal["hint_diagnostics"] == [
        "target_ceiling is a compatibility hint, not an authorization fact"
    ]


def test_multi_grant_revoke_receipt_uses_sorted_component_identity_hashes(
    vault: Path,
) -> None:
    from exomem.governance import receipts, tokens
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    for _ in range(2):
        token = tokens.mint(
            vault,
            paths=["Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"],
            audience="external",
            max_level=5,
            authorization_session="conversation-a",
        )
        op_govern_memory(
            vault,
            operation="grant",
            principal=_external(),
            authorization_session="conversation-a",
            token=token,
        )
    revoked = op_govern_memory(
        vault,
        operation="revoke",
        principal=_external(),
        authorization_session="conversation-a",
        scope="session",
    )
    assert revoked["revoked"] == 2
    with sqlite3.connect(store.sidecar_path(vault)) as conn:
        expected = __import__("json").loads(
            conn.execute(
                "SELECT affected_ids FROM governance_operation_journals "
                "WHERE event_id=?",
                (revoked["event_id"],),
            ).fetchone()[0]
        )
    intent = next(
        record
        for record in receipts.event_records(vault)
        if record.get("event_id") == revoked["event_id"]
    )
    assert intent["affected_ids"] == expected == sorted(set(expected))


# ---------------------------------------------------------------------------
# `explain` for a default denial (add-default-deny-scope-cap, task 3)
# ---------------------------------------------------------------------------

DECLARED_RESTRICTED = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"


def _write_declared_scope(vault: Path, *, default_deny: bool = True) -> None:
    """A scope carrying the declaration and NO rule naming `external`."""
    scopes = vault / "Knowledge Base" / "_Governance" / "scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "patterns.yaml").write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Confidential patterns\n"
        f'paths: ["{PATTERN_GLOB}"]\n'
        + ("default_deny: true\n" if default_deny else ""),
        encoding="utf-8",
    )


def test_explain_names_the_declaring_scope_for_a_default_denial(vault: Path) -> None:
    """Spec: the explanation identifies the declaring scope, and does NOT
    attribute the outcome to a standing rule that does not exist.

    A declared scope with no rules is invisible until someone is denied, so
    `explain` reporting a bare "nothing matched" would leave the owner unable
    to tell a default denial from a missing item."""
    from exomem.governance.tool import op_govern_memory

    _write_declared_scope(vault)
    explained = op_govern_memory(
        vault,
        operation="explain",
        principal=owner_principal(),
        audience="external",
        path=DECLARED_RESTRICTED,
    )
    assert explained["effective_ceiling"] == 0
    # No rule was invented to carry the outcome.
    assert explained["rule_ids"] == []
    assert explained["participating_chain"] == []
    assert explained["default_deny_scope_ids"] == [SCOPE_ID]


def test_explain_reports_no_declaring_scope_when_a_rule_decided(vault: Path) -> None:
    """The distinguishing half: an authored denial must not be dressed up as a
    default one, or the field means nothing."""
    from exomem.governance.tool import op_govern_memory

    _committed_policy(vault)
    explained = op_govern_memory(
        vault,
        operation="explain",
        principal=owner_principal(),
        audience="external",
        path=DECLARED_RESTRICTED,
    )
    assert explained["rule_ids"] == [RULE_ID]
    assert "default_deny_scope_ids" not in explained


def _scope_document(*, default_deny: bool) -> str:
    return (
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Confidential patterns\n"
        f'paths: ["{PATTERN_GLOB}"]\n'
        + ("default_deny: true\n" if default_deny else "")
    )


def _write_declared_scope_with_a_rule(vault: Path, *, default_deny: bool) -> None:
    """A declared scope PLUS an unrelated authored audience.

    The rule is what makes the failure visible rather than vacuous: with a
    named audience in the policy the lattice is non-empty, so a direction is
    computed from it — and the audience whose ceiling actually moved is the one
    no document names.
    """
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "patterns.yaml").write_text(
        _scope_document(default_deny=default_deny), encoding="utf-8"
    )
    (governance / "rules" / "patterns.yaml").write_text(
        f"governance_version: 1\nid: {RULE_ID}\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 1\n',
        encoding="utf-8",
    )


def test_removing_default_deny_is_classified_as_a_widening(vault: Path) -> None:
    """The owner's only review signal before committing must not misreport the
    exact edit that undoes this feature.

    The declaration names no audience, so enumerating audiences from rules and
    grants alone never evaluates the one whose ceiling moves 0 -> 6. The
    default itself has to be in the compared lattice.
    """
    from exomem.governance.tool import _effective_transition_direction

    _write_declared_scope_with_a_rule(vault, default_deny=True)

    direction = _effective_transition_direction(
        vault, {"scopes/patterns.yaml": _scope_document(default_deny=False)}
    )

    assert direction == "widening"


def test_adding_default_deny_is_classified_as_a_narrowing(vault: Path) -> None:
    """The other half of the pair: the declaration is a restriction, and the
    lattice it is measured in must be able to see that."""
    from exomem.governance.tool import _effective_transition_direction

    _write_declared_scope_with_a_rule(vault, default_deny=False)

    direction = _effective_transition_direction(
        vault, {"scopes/patterns.yaml": _scope_document(default_deny=True)}
    )

    assert direction == "narrowing"


def test_v4_proposal_analysis_never_calls_release_grant_change_narrowing(
    vault: Path,
) -> None:
    from exomem.governance import policy
    from exomem.governance.tool import _proposal_analysis

    current = policy.Policy(fingerprint="a" * 64)
    release = policy.ReleaseGrant(
        id="01ARZ3NDEKTSV4RRFFQ69G5FZZ",
        source="grants/release.yaml",
        path="Knowledge Base/Notes/released.md",
        ref="mem:01ARZ3NDEKTSV4RRFFQ69G5FZY",
        content_hash="b" * 64,
        to_audience="external",
        released_at="2026-08-27T00:00:00Z",
        why="reviewed release",
        bridge_scope="exact",
        bridge_of=(),
        strip_provenance=(),
    )
    prospective = dataclasses.replace(
        current,
        fingerprint="c" * 64,
        release_grants=(release,),
    )

    assert _proposal_analysis(vault, current, prospective, [])[2] == "widening"


def test_a_proposal_removing_default_deny_counts_the_widened_audience(
    vault: Path,
) -> None:
    """`propose` reports the consequences the owner reviews. A removal that
    reopens every unnamed audience must not read as `widened: 0`."""
    from exomem.governance.tool import op_govern_memory

    _write_declared_scope_with_a_rule(vault, default_deny=True)

    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Stop denying audiences the policy does not name",
        documents={"scopes/patterns.yaml": _scope_document(default_deny=False)},
        selector_paths=[],
        target_ceiling=6,
    )

    assert proposal["consequences"]["direction"] == "widening"
    assert proposal["consequences"]["widened"] > 0
    assert proposal["consequences"]["target_ceiling"] == 1
    assert proposal["consequences"]["unnamed_audience_ceiling"] == 6


def test_a_proposal_adding_default_deny_counts_the_narrowed_audience(
    vault: Path,
) -> None:
    from exomem.governance.tool import op_govern_memory

    _write_declared_scope_with_a_rule(vault, default_deny=False)

    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Deny audiences the policy does not name",
        documents={"scopes/patterns.yaml": _scope_document(default_deny=True)},
        selector_paths=[],
        target_ceiling=0,
    )

    assert proposal["consequences"]["direction"] == "narrowing"
    assert proposal["consequences"]["narrowed"] > 0
    assert proposal["consequences"]["widened"] == 0


@pytest.mark.parametrize(
    ("operation", "argument"),
    [("explain", "path"), ("simulate", "paths")],
)
def test_non_owner_inspection_cannot_distinguish_default_denied_from_missing(
    vault: Path, operation: str, argument: str
) -> None:
    """Same input, varied condition: the caller asks about one path while it
    exists behind a declared default, then asks again after it is deleted."""
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _write_declared_scope(vault)
    value: object = DECLARED_RESTRICTED if argument == "path" else [DECLARED_RESTRICTED]

    def outcome() -> tuple[str, str, str]:
        try:
            result = op_govern_memory(
                vault,
                operation=operation,
                principal=_external(),
                audience="external",
                **{argument: value},
            )
        except GovernanceError as error:
            return type(error).__name__, error.code, str(error)
        return "ok", "", repr(result)

    present = outcome()
    (vault / DECLARED_RESTRICTED).unlink()
    missing = outcome()

    assert present == missing
    assert present == (
        "GovernanceError",
        "INVALID_INSPECTION_PATH",
        "INVALID_INSPECTION_PATH: path must be canonical",
    )


def test_non_owner_inspection_remains_available_when_a_grant_raises_the_default(
    vault: Path,
) -> None:
    """The oracle guard applies only while the declared floor remains L0."""
    from exomem.governance.tool import op_govern_memory

    _write_declared_scope(vault)
    grants = vault / "Knowledge Base" / "_Governance" / "grants"
    grants.mkdir(parents=True, exist_ok=True)
    (grants / "external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\nkind: standing\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 3\n',
        encoding="utf-8",
    )

    explained = op_govern_memory(
        vault,
        operation="explain",
        principal=_external(),
        audience="external",
        path=DECLARED_RESTRICTED,
    )
    simulated = op_govern_memory(
        vault,
        operation="simulate",
        principal=_external(),
        audience="external",
        paths=[DECLARED_RESTRICTED],
    )

    assert explained["effective_ceiling"] == 3
    assert simulated["evaluated_count"] == 1
