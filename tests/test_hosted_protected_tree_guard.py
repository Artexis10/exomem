"""Hosted profiles must not hand a tenant a write primitive over its own doctrine.

`hosted-alpha-agent-v1` kept `_Schema` safe purely by *not exposing*
`edit_memory` or `replace_memory`. `hosted-alpha-agent-v3` exposes both, so that
protection has to be re-established as an actual control. These tests drive the
real hosted forwarding route -- trusted-context auth, coercion, profile
resolution, lifecycle admission, real command leaf -- not the leaf directly.

Local and CLI behaviour is deliberately untouched: a single-user vault
legitimately customises its own `_Schema`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from exomem import commands as commands_module
from exomem import hosted_gateway as gateway
from exomem import schema
from exomem.hosted_runtime import HostedCellConfig, HostedCellLifecycle, HostedResourceLimits
from exomem.server_hosted import register_hosted_routes

V3_PROFILE = "hosted-alpha-agent-v3"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = (
    base64.urlsafe_b64encode(hashlib.sha256(b"protected-tree-principal").digest())
    .rstrip(b"=")
    .decode()
)


class _ProfileConfig(HostedCellConfig):
    """A cell whose operator has selected the epistemic profile.

    Only the profile *selection* input is stubbed. Everything downstream --
    routing, auth, coercion, admission, the command leaf -- is the real thing.
    """

    @property
    def active_agent_profile(self) -> str:
        return V3_PROFILE


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


@contextmanager
def _mutation_guard(_vault_root: Path) -> Iterator[None]:
    yield


def _cell(tmp_path: Path, *, profile: str = V3_PROFILE) -> tuple[Any, HostedCellConfig]:
    from exomem.init import init_vault

    vault_root = tmp_path / "vault"
    init_vault(vault_root)
    factory = _ProfileConfig if profile == V3_PROFILE else HostedCellConfig
    config = factory(
        cell_id="cell-protected-tree",
        vault_root=vault_root,
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential="protected-tree-private-service-credential-01",
        enforce_transfer_v1_compatibility=False,
        records_reader_version=2,
        lifecycle_actions_enabled=(profile == commands_module.HOSTED_ALPHA_AGENT_V2_PROFILE),
        resource_limits=HostedResourceLimits(
            storage_bytes=4 * 1024 * 1024, upload_bytes=4096, worker_count=0
        ),
    )
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True, mutation_authority_ready=True, service_auth_ready=True
    )
    app = FastMCP("test-protected-tree")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=schema.load_source_schema(vault_root),
        mutation_guard_factory=_mutation_guard,
    )
    return app.http_app(), config


def _headers(config: HostedCellConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.service_credential}",
        gateway.CELL_HEADER: config.cell_id,
        gateway.PROTOCOL_HEADER: config.protocol_version,
        gateway.REQUEST_HEADER: REQUEST_ID,
        gateway.PRINCIPAL_HEADER: PRINCIPAL,
    }


def _call(app: Any, config: HostedCellConfig, command: str, body: dict[str, Any]) -> httpx.Response:
    return _request(
        app,
        "POST",
        f"/private/exomem/v1/agent/{V3_PROFILE}/command/{command}",
        json=body,
        headers=_headers(config),
    )


def _kb() -> str:
    from exomem.kbdir import kb_dirname

    return kb_dirname()


def _schema_doc(vault_root: Path, name: str = "SKILL.md") -> Path:
    return vault_root / _kb() / "_Schema" / name


# --- The two commands v3 adds -------------------------------------------------


def test_hosted_v3_refuses_edit_memory_against_the_schema_tree(tmp_path: Path) -> None:
    app, config = _cell(tmp_path)
    target = _schema_doc(config.vault_root)
    assert target.is_file(), "vault scaffold should ship a governing schema document"
    before = target.read_bytes()

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": f"{target.relative_to(config.vault_root).as_posix()}",
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert target.read_bytes() == before


def test_hosted_v3_refuses_replace_memory_against_the_schema_tree(tmp_path: Path) -> None:
    app, config = _cell(tmp_path)
    target = _schema_doc(config.vault_root)
    before = target.read_bytes()

    response = _call(
        app,
        config,
        "replace_memory",
        {
            "old_path": target.relative_to(config.vault_root).as_posix(),
            "content": "hijacked doctrine",
            "title": "Hijacked",
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert target.read_bytes() == before


# --- The bypasses a path guard is usually wrong about -------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "Knowledge Base/_schema/SKILL.md",
        "Knowledge Base/_SCHEMA/SKILL.md",
        "Knowledge Base/_Schema/",
        "Knowledge Base/_Schema//references/frontmatter.md",
        "Knowledge Base/_Schema/references/page-types.md",
        "Knowledge Base/Notes/../_Schema/SKILL.md",
        "Knowledge Base\\_Schema\\SKILL.md",
        "./Knowledge Base/_Schema/SKILL.md",
        "Knowledge Base/_Governance/rules/policy.md",
        "Knowledge Base/_governance/scopes/scope.md",
    ],
)
def test_hosted_v3_protected_tree_refusal_is_not_bypassable(
    tmp_path: Path, candidate: str
) -> None:
    app, config = _cell(tmp_path)

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": candidate,
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text


def test_hosted_v3_refuses_an_absolute_path_resolving_inside_the_schema_tree(
    tmp_path: Path,
) -> None:
    app, config = _cell(tmp_path)
    absolute = _schema_doc(config.vault_root).as_posix()

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": absolute,
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked\n"},
        },
    )

    assert response.status_code >= 400, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text


# --- What must keep working ---------------------------------------------------


def test_hosted_v3_still_edits_ordinary_governed_pages(tmp_path: Path) -> None:
    """The guard must be a scalpel, not a blanket refusal of `edit_memory`."""

    app, config = _cell(tmp_path)
    body = (
        "A durable conclusion used as a positive control.\n\n"
        "## Observations\n\n- [operating constraint] Keep retries bounded #reliability\n"
    )
    created = commands_module.op_remember(
        config.vault_root, title="Ordinary conclusion", content=body, note_type="insight"
    )
    operation = {
        "kind": "replace_string",
        "old_string": "Keep retries bounded",
        "new_string": "Keep retries bounded and logged",
        "validate_only": True,
    }

    ordinary = _call(
        app,
        config,
        "edit_memory",
        {"path": created["path"], "why": "ordinary page stays editable", "operation": operation},
    )
    protected = _call(
        app,
        config,
        "edit_memory",
        {
            "path": f"{_kb()}/_Schema/SKILL.md",
            "why": "doctrine must not be editable",
            "operation": operation,
        },
    )

    # The ordinary page reaches the command; the protected one never does.
    assert ordinary.status_code < 400, ordinary.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" not in ordinary.text
    assert protected.status_code == 403, protected.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in protected.text


def test_hosted_v3_still_reads_the_schema_tree(tmp_path: Path) -> None:
    """The guard is a mutation control, not a read control."""

    app, config = _cell(tmp_path)
    target = _schema_doc(config.vault_root)

    response = _call(
        app,
        config,
        "read_memory",
        {"path": target.relative_to(config.vault_root).as_posix()},
    )

    assert "HOSTED_PROTECTED_TREE_MUTATION" not in response.text


def test_local_surface_still_customises_its_own_schema(tmp_path: Path) -> None:
    """A single-user local vault owns its `_Schema`; the guard must not reach it."""

    from exomem.init import init_vault

    vault_root = tmp_path / "local-vault"
    init_vault(vault_root)
    target = _schema_doc(vault_root)
    before = target.read_text(encoding="utf-8")

    commands_module.op_edit_memory(
        vault_root,
        path=target.relative_to(vault_root).as_posix(),
        why="local operator customises the vault contract",
        operation={"kind": "replace_body", "new_body": before + "\nLocal addition.\n"},
    )

    assert "Local addition." in target.read_text(encoding="utf-8")


def test_governance_readme_is_a_real_prevented_write(tmp_path: Path) -> None:
    """`_Governance` was reachable too -- an existing policy file proves it."""

    app, config = _cell(tmp_path)
    target = config.vault_root / _kb() / "_Governance" / "README.md"
    assert target.is_file(), "vault scaffold should ship a policy-tree document"
    before = target.read_bytes()

    response = _call(
        app,
        config,
        "edit_memory",
        {
            "path": target.relative_to(config.vault_root).as_posix(),
            "why": "probe",
            "operation": {"kind": "replace_body", "new_body": "# hijacked policy\n"},
        },
    )

    assert response.status_code == 403, response.text
    assert "HOSTED_PROTECTED_TREE_MUTATION" in response.text
    assert target.read_bytes() == before


def test_a_profile_with_an_unclassified_mutation_refuses_to_serve() -> None:
    """Fail closed: the next widening cannot reopen this hole by silence."""

    from types import MappingProxyType

    original = commands_module.PRODUCT_SURFACE_PROFILES
    widened = dict(original)
    widened["hosted-test-unclassified"] = commands_module.ProductSurfaceProfile(
        name="hosted-test-unclassified",
        command_names=("bootstrap", "ask_memory", "schema_memory"),
    )
    commands_module.PRODUCT_SURFACE_PROFILES = MappingProxyType(widened)
    try:
        with pytest.raises(gateway.HostedGatewayError) as raised:
            gateway.assert_profile_mutations_are_classified("hosted-test-unclassified")
        assert raised.value.code == "HOSTED_SURFACE_PROFILE_UNSUPPORTED"

        # Every currently registered profile is fully classified.
        for profile in original:
            gateway.assert_profile_mutations_are_classified(profile)
    finally:
        commands_module.PRODUCT_SURFACE_PROFILES = original
