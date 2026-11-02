"""A client-bound writer for a test whose restricted audience must write.

A non-owner audience under a governed vault holds no write authority unless a client entry
in the host configuration lists the scopes it may write. A test that proves what such an
audience's write does or does not reveal therefore reaches the audience through one client
entry: its writable scope selects the paths the test writes, and the capture path is its
inbox. Everything the test withholds stays in the client's denied set.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from conftest import initialize_vault_state_offline

from exomem import state_migration
from exomem.activation_manifest import ensure_manifest
from exomem.governance import connector_boundary, egress, membership, policy
from exomem.governance.principal import (
    ClientBinding,
    OriginSessionBinding,
    RequestPrincipal,
    owner_principal,
    request_scope,
)

ISSUER = "https://memory.example.test"
GRANT_ID = "01ARZ3NDEKTSV4RRFFQ69G5GW1"
FENCE_ID = "01ARZ3NDEKTSV4RRFFQ69G5GW3"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5GW2"
CLIENT = "writer"


def grant_writer(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    writable_paths: Sequence[str],
    capture_paths: Sequence[str] = (),
    protected: Sequence[str] = (),
    writable_scopes: Sequence[str] = (),
    config_dir: Path | None = None,
    audience: str = "external",
) -> Callable[[str], RequestPrincipal]:
    """Arm the connector boundary for one writer client; return the audience-to-principal factory.

    `writable_paths` are the knowledge-base-relative globs of the grant scope and
    `capture_paths` the vault-relative folders that are the writer's inbox. `protected` lists the
    scope ids the vault withholds from the audience. A page the test's own scopes also select is
    writable only when its scope ids are in `writable_scopes`. The client always denies a scope
    that selects nothing, because only a fenced client may write. Call it after the test has
    written its own policy, because arming pins the protective scopes. The host configuration
    goes in `config_dir`, which defaults to `tmp_path` and must lie outside the vault. A page in a
    scope that no rule covers is withheld, so the `external` audience gets a full-release rule for
    the granted scope, or `audience` when the test's writer is another audience; a stricter rule
    of the test's own scopes still lowers the ceiling.
    """
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "granted.yaml").write_text(
        f"governance_version: 1\nid: {GRANT_ID}\nname: Granted\npaths: {json.dumps(list(writable_paths))}\n",
        encoding="utf-8",
    )
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "rules" / "granted.yaml").write_text(
        f"governance_version: 1\nid: {RULE_ID}\nscope_ids: [\"{GRANT_ID}\"]\n"
        f"audience: {audience}\nceiling: {egress.LEVEL_FULL}\n",
        encoding="utf-8",
    )
    (governance / "scopes" / "fence.yaml").write_text(
        f'governance_version: 1\nid: {FENCE_ID}\nname: Fence\npaths: ["_nothing-is-filed-here/**"]\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(connector_boundary.CONFIG_ENV, raising=False)
    try:
        state_migration.require_vault_state_ready(vault)
    except state_migration.StateMigrationOfflineRequired:
        initialize_vault_state_offline(vault, source="granted writer vault")
    with request_scope(owner_principal(surface="library")):
        ensure_manifest(vault)
    config = (config_dir or tmp_path) / "host-boundary.json"
    writable = [GRANT_ID, *writable_scopes]
    config.write_text(json.dumps({
        "version": 1, "default_denied_scope_ids": [*protected, FENCE_ID, *writable], "capture_paths": list(capture_paths),
        "clients": [{"issuer": ISSUER, "client_id": CLIENT, "denied_scope_ids": [*protected, FENCE_ID],
                     "writable_scope_ids": writable}],
    }), encoding="utf-8")
    monkeypatch.setenv(connector_boundary.CONFIG_ENV, str(config))
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    authority = state_migration.assert_offline_migration_authority(source="granted writer stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)

    def writer(audience: str) -> RequestPrincipal:
        return RequestPrincipal(
            audience_id=audience, surface="mcp", resolved=True,
            client_binding=ClientBinding(ISSUER, CLIENT),
            origin_session=OriginSessionBinding("session", "generation", audience),
        )

    return writer
