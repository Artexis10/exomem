from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastmcp import FastMCP

from exomem import commands as commands_module
from exomem import hosted_gateway, schema
from exomem.hosted_runtime import HostedCellConfig, HostedCellLifecycle, HostedConfigError
from exomem.server_hosted import register_hosted_routes


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": "cell-alpha",
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "EXOMEM_HOSTED_STATE_ROOT": str(tmp_path / "state"),
        "EXOMEM_LOG_DIR": str(tmp_path / "logs"),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": "hosted-profile-selection-credential",
        "EXOMEM_HOSTED_RECORDS_READER_VERSION": "2",
        "EXOMEM_HOSTED_LIFECYCLE_ACTIONS_ENABLED": "false",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "profile",
    (
        commands_module.HOSTED_ALPHA_AGENT_V3_PROFILE,
        commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE,
    ),
)
def test_hosted_config_selects_an_explicit_registered_agent_profile(
    tmp_path: Path, profile: str
) -> None:
    config = HostedCellConfig.from_env(
        _env(tmp_path, EXOMEM_HOSTED_AGENT_PROFILE=profile), require_provisioned=False
    )

    assert config.active_agent_profile == profile


def test_hosted_config_rejects_an_unknown_explicit_agent_profile(tmp_path: Path) -> None:
    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(
            _env(tmp_path, EXOMEM_HOSTED_AGENT_PROFILE="hosted-alpha-agent-v999"),
            require_provisioned=False,
        )

    assert error.value.code == "HOSTED_AGENT_PROFILE_UNSUPPORTED"


def test_direct_hosted_config_rejects_an_unknown_explicit_agent_profile(tmp_path: Path) -> None:
    config = replace(
        HostedCellConfig.from_env(_env(tmp_path), require_provisioned=False),
        agent_profile="hosted-alpha-agent-v999",
    )

    with pytest.raises(HostedConfigError) as error:
        _ = config.active_agent_profile

    assert error.value.code == "HOSTED_AGENT_PROFILE_UNSUPPORTED"


def test_direct_hosted_config_rejects_lifecycle_actions_with_reader_one(
    tmp_path: Path,
) -> None:
    from exomem.init import init_vault

    values = _env(tmp_path)
    init_vault(Path(values["EXOMEM_VAULT_PATH"]))
    config = replace(
        HostedCellConfig.from_env(values, require_provisioned=False),
        agent_profile=commands_module.HOSTED_ALPHA_AGENT_PROFILE,
        records_reader_version=1,
        lifecycle_actions_enabled=True,
    )

    with pytest.raises(HostedConfigError) as error:
        register_hosted_routes(
            FastMCP("direct-config-lifecycle-guard"),
            config=config,
            lifecycle=HostedCellLifecycle(config),
            source_schema=schema.load_source_schema(config.vault_root),
        )

    assert error.value.code == "HOSTED_RECORDS_READER_UNSUPPORTED"


def test_hosted_config_requires_records_reader_two_for_a_records_profile(tmp_path: Path) -> None:
    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(
            _env(
                tmp_path,
                EXOMEM_HOSTED_AGENT_PROFILE=commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE,
                EXOMEM_HOSTED_RECORDS_READER_VERSION="1",
            ),
            require_provisioned=False,
        )

    assert error.value.code == "HOSTED_RECORDS_READER_UNSUPPORTED"


@pytest.mark.parametrize(
    ("lifecycle_actions_enabled", "expected_profile"),
    (
        ("false", commands_module.HOSTED_ALPHA_AGENT_PROFILE),
        ("true", commands_module.HOSTED_ALPHA_AGENT_V2_PROFILE),
    ),
)
def test_hosted_config_preserves_legacy_profile_fallback(
    tmp_path: Path, lifecycle_actions_enabled: str, expected_profile: str
) -> None:
    config = HostedCellConfig.from_env(
        _env(tmp_path, EXOMEM_HOSTED_LIFECYCLE_ACTIONS_ENABLED=lifecycle_actions_enabled),
        require_provisioned=False,
    )

    assert config.active_agent_profile == expected_profile


def test_configured_v4_profile_exposes_its_canonical_authenticated_contract(tmp_path: Path) -> None:
    from exomem.init import init_vault

    values = _env(
        tmp_path,
        EXOMEM_HOSTED_AGENT_PROFILE=commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE,
    )
    init_vault(Path(values["EXOMEM_VAULT_PATH"]))
    config = HostedCellConfig.from_env(values, require_provisioned=False)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True, mutation_authority_ready=True, service_auth_ready=True
    )
    app = FastMCP("hosted-profile-selection")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=schema.load_source_schema(config.vault_root),
    )

    headers = {
        "Authorization": f"Bearer {config.service_credential}",
        hosted_gateway.CELL_HEADER: config.cell_id,
        hosted_gateway.PROTOCOL_HEADER: config.protocol_version,
        hosted_gateway.REQUEST_HEADER: "11111111-1111-4111-8111-111111111111",
        hosted_gateway.PRINCIPAL_HEADER: base64.urlsafe_b64encode(
            hashlib.sha256(b"profile-selection-principal").digest()
        )
        .rstrip(b"=")
        .decode(),
    }

    async def request() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app.http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            contract = await client.get(
                f"/private/exomem/v1/agent/{config.active_agent_profile}/contract", headers=headers
            )
            lifecycle_action = await client.post(
                f"/private/exomem/v1/agent/{config.active_agent_profile}/command/record_memory",
                headers=headers,
                json={"action": "revise"},
            )
            return contract, lifecycle_action

    response, lifecycle_action = asyncio.run(request())

    assert response.status_code == 200
    assert response.json() == hosted_gateway.build_agent_gateway_contract(
        profile=commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE,
        protocol_version=config.protocol_version,
    )
    assert lifecycle_action.status_code == 400
    assert lifecycle_action.json()["error"]["code"] == "HOSTED_RECORDS_LIFECYCLE_DISABLED"


def test_provisioned_v4_profile_reaches_the_authenticated_runtime_contract(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    helm = os.environ.get("HELM_BIN") or shutil.which("helm")
    assert helm, "Helm is required to render the cell chart"

    producer = """
import json
import sys
from dataclasses import replace

sys.path.insert(0, {tests!r})
from test_provider_lifecycle import _config, _metadata, _request_with_provider_identity, _runtime_target
from exomem_provisioner.lifecycle import _fixed_helm_values

target = _runtime_target(agentProfile={profile!r})
config = replace(_config(), runtime_target=target, records_reader_version=2)
request = _request_with_provider_identity()
request.pop("releaseVersion")
request.pop("protocolVersion")
request["runtimeTarget"] = target
print(json.dumps(_fixed_helm_values(_metadata(), request, config)))
""".format(
        tests=str(root / "infra/provisioner/tests"),
        profile=commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE,
    )
    produced = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(root / "infra/provisioner"),
            "--frozen",
            "--python",
            f"{sys.version_info.major}.{sys.version_info.minor}",
            "python",
            "-c",
            producer,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "provisioner-environment"),
        },
    )
    values = json.loads(produced.stdout)
    assert values["agentProfile"] == commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE

    chart_values = dict(values)
    chart_values["authorizationSessionRevision"] = "a" * 64
    chart_values["workloadMode"] = "serve"
    values_path = tmp_path / "rendered-values.yaml"
    values_path.write_text(yaml.safe_dump(chart_values), encoding="utf-8")

    rendered = subprocess.run(
        [
            str(helm),
            "template",
            "profile-selection",
            str(root / "infra/helm/cell"),
            "--namespace",
            "cell-alpha-test",
            "--values",
            str(values_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    statefulset = next(
        document
        for document in yaml.safe_load_all(rendered.stdout)
        if document.get("kind") == "StatefulSet"
    )
    environment = {
        item["name"]: item["value"]
        for item in statefulset["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    environment["EXOMEM_AUTH_SESSION_REPLICA_ID"] = "cell-alpha-0"
    rendered_config = HostedCellConfig.from_env(environment, require_provisioned=False)
    assert rendered_config.active_agent_profile == commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE

    from exomem.init import init_vault

    vault_root = tmp_path / "vault"
    init_vault(vault_root)
    config = replace(
        rendered_config,
        vault_root=vault_root,
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        runtime_uid=os.geteuid(),
        runtime_gid=os.getegid(),
        enforce_transfer_v1_compatibility=False,
    )
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True, mutation_authority_ready=True, service_auth_ready=True
    )
    credential = "hosted-profile-selection-dynamic-credential"

    class DynamicAuthority:
        def authenticate(self, presented: str | None) -> object | None:
            if presented == credential:
                return SimpleNamespace(credential_version="active-v1", security_revision=1)
            return None

    app = FastMCP("rendered-profile-selection")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=schema.load_source_schema(vault_root),
        private_authenticator=DynamicAuthority(),
    )
    headers = {
        "Authorization": f"Bearer {credential}",
        hosted_gateway.CELL_HEADER: config.cell_id,
        hosted_gateway.PROTOCOL_HEADER: config.protocol_version,
        hosted_gateway.REQUEST_HEADER: "11111111-1111-4111-8111-111111111111",
        hosted_gateway.PRINCIPAL_HEADER: base64.urlsafe_b64encode(
            hashlib.sha256(b"rendered-profile-selection-principal").digest()
        )
        .rstrip(b"=")
        .decode(),
    }

    async def request() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app.http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            selected = await client.get(
                f"/private/exomem/v1/agent/{config.active_agent_profile}/contract", headers=headers
            )
            unselected = await client.get(
                "/private/exomem/v1/agent/hosted-alpha-agent-v1/contract", headers=headers
            )
            return selected, unselected

    selected, unselected = asyncio.run(request())

    assert selected.status_code == 200
    assert selected.json() == hosted_gateway.build_agent_gateway_contract(
        profile=commands_module.HOSTED_ALPHA_AGENT_V4_PROFILE,
        protocol_version=config.protocol_version,
    )
    assert unselected.status_code == 400
    assert unselected.json()["error"]["code"] == "HOSTED_SURFACE_PROFILE_UNSUPPORTED"
