"""End-to-end contract tests for the manual cold-standby entrypoint."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

PWSH = shutil.which("pwsh")
CLOUDFLARED = shutil.which("cloudflared")
SCRIPT = Path(__file__).parents[1] / "scripts" / "cold-standby.ps1"
ACK = "I ACKNOWLEDGE SPLIT-BRAIN AND DATA-LOSS RISK"
DESKTOP_TUNNEL_ID = "11111111-1111-4111-8111-111111111111"
LAPTOP_TUNNEL_ID = "22222222-2222-4222-8222-222222222222"

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh is not available")


def _config(tmp_path: Path, *, role: str = "laptop") -> tuple[Path, dict[str, object]]:
    peer = "desktop" if role == "laptop" else "laptop"
    local_tunnel_id = LAPTOP_TUNNEL_ID if role == "laptop" else DESKTOP_TUNNEL_ID
    peer_tunnel_id = DESKTOP_TUNNEL_ID if role == "laptop" else LAPTOP_TUNNEL_ID
    config: dict[str, object] = {
        "schema_version": 1,
        "host": {"id": role, "role": role, "peer_id": peer},
        "health": {
            "local_url": f"https://{role}.ops.example.test/health/ready",
            "peer_url": f"https://{peer}.ops.example.test/health/ready",
            "stable_url": "https://exomem.example.test/health/ready",
            "oauth_discovery_url": "https://exomem.example.test/.well-known/oauth-authorization-server",
        },
        "services": {"exomem": "Exomem", "cloudflared": "cloudflared"},
        "tunnels": {
            "local_name": f"{role}-tunnel",
            "local_id": local_tunnel_id,
            "peer_name": f"{peer}-tunnel",
            "peer_id": peer_tunnel_id,
            "stable_hostname": "exomem.example.test",
            "local_operational_hostname": f"{role}.ops.example.test",
            "peer_operational_hostname": f"{peer}.ops.example.test",
            "config_path": str(tmp_path / f"cloudflared-{role}.yml"),
            "origin_service_url": "http://127.0.0.1:8765",
        },
        "cloudflare": {
            "api_token_environment": "TEST_CLOUDFLARE_API_TOKEN",
            "zone_id_environment": "TEST_CLOUDFLARE_ZONE_ID",
        },
        "syncthing": {
            "api_url": "http://127.0.0.1:8384",
            "api_key_environment": "TEST_SYNCTHING_API_KEY",
            "folder_id": "vault",
            "folder_path": str(tmp_path / "syncthing-shared"),
            "peer_device_id": "peer-device-id",
            "intent_relative_path": ".exomem-state/desired-host.json",
        },
        "state": {
            "journal_path": str(tmp_path / f"journal-{role}.json"),
            "intent_path": str(tmp_path / "syncthing-shared" / ".exomem-state" / "desired-host.json"),
            "manifest_path": str(tmp_path / "syncthing-shared" / ".exomem-state" / f"manifest-{role}.json"),
            "peer_manifest_path": str(tmp_path / "syncthing-shared" / ".exomem-state" / f"manifest-{peer}.json"),
        },
        "identity_environment": {
            "dotenv_path": str(tmp_path / f"service-{role}.env"),
            "base_url": "TEST_BASE_URL",
            "github_client_id": "TEST_GITHUB_CLIENT_ID",
            "github_client_secret": "TEST_GITHUB_CLIENT_SECRET",
            "github_username": "TEST_GITHUB_USERNAME",
            "github_user_id": "TEST_GITHUB_USER_ID",
            "jwt_signing_key": "TEST_JWT_SIGNING_KEY",
            "instance_id": "TEST_INSTANCE_ID",
        },
        "operations": {
            "probe_attempts": 3,
            "probe_delay_seconds": 0,
            "desktop_logon_delay_seconds": 30,
        },
    }
    Path(str(config["identity_environment"]["dotenv_path"])).write_text(
        "\n".join(
            [
                "TEST_BASE_URL=https://exomem.example.test/",
                "TEST_GITHUB_CLIENT_ID=client-id",
                "TEST_GITHUB_CLIENT_SECRET=client-secret",
                "TEST_GITHUB_USERNAME=operator-login",
                "TEST_GITHUB_USER_ID=123456",
                "TEST_JWT_SIGNING_KEY=not-for-output",
                f"TEST_INSTANCE_ID={role}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path = tmp_path / f"config-{role}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path, config


def _default_state(config: dict[str, object]) -> dict[str, object]:
    host = config["host"]
    tunnels = config["tunnels"]
    assert isinstance(host, dict) and isinstance(tunnels, dict)
    role = str(host["id"])
    peer = str(host["peer_id"])
    return {
        "services": {role: "stopped", peer: "stopped"},
        "service_environments": {
            role: {
                "state": "known",
                "values": {
                    "TEST_BASE_URL": "https://exomem.example.test/",
                    "TEST_GITHUB_CLIENT_ID": "client-id",
                    "TEST_GITHUB_CLIENT_SECRET": "client-secret",
                    "TEST_GITHUB_USERNAME": "operator-login",
                    "TEST_GITHUB_USER_ID": "123456",
                    "TEST_JWT_SIGNING_KEY": "not-for-output",
                    "TEST_INSTANCE_ID": role,
                },
            },
            peer: {
                "state": "known",
                "values": {
                    "TEST_BASE_URL": "https://exomem.example.test/",
                    "TEST_GITHUB_CLIENT_ID": "client-id",
                    "TEST_GITHUB_CLIENT_SECRET": "client-secret",
                    "TEST_GITHUB_USERNAME": "operator-login",
                    "TEST_GITHUB_USER_ID": "123456",
                    "TEST_JWT_SIGNING_KEY": "not-for-output",
                    "TEST_INSTANCE_ID": peer,
                },
            },
        },
        "route": {
            "state": "known",
            "tunnel_id": str(tunnels["peer_id"]),
            "target": f"{tunnels['peer_id']}.cfargotunnel.com",
        },
        "probe_override": {},
        "probe_sequences": {},
        "oauth_override": {},
        "syncthing": {
            "api": "reachable",
            "folder_state": "idle",
            "pending_items": 0,
            "pending_bytes": 0,
            "peer_completion": 100,
            "peer_complete": True,
        },
        "tunnels": {
            str(tunnels["local_name"]): "connected",
            str(tunnels["peer_name"]): "connected",
        },
        "tunnel_config": "needs_update",
        "tunnel_service_binding": "ready",
        "intent_delivery": "delivered",
        "set_route": "ok",
        "start_result": "ok",
        "stop_result": "ok",
        "trace": [],
    }


def _write_matching_manifests(config: dict[str, object]) -> None:
    state = config["state"]
    assert isinstance(state, dict)
    fingerprints = {
        "stable_base_url": _sha256("https://exomem.example.test"),
        "github_oauth_client_id": _sha256("client-id"),
        "github_oauth_client_secret": _sha256("client-secret"),
        "github_oauth_callback": _sha256("https://exomem.example.test/auth/callback"),
        "allowed_github_username": _sha256("operator-login"),
        "allowed_github_user_id": _sha256("123456"),
        "jwt_signing_key": _sha256("not-for-output"),
    }
    for key in ("manifest_path", "peer_manifest_path"):
        Path(str(state[key])).parent.mkdir(parents=True, exist_ok=True)
        Path(str(state[key])).write_text(
            json.dumps({"schema_version": 1, "host_id": Path(str(state[key])).stem.removeprefix("manifest-"), "fingerprints": fingerprints}),
            encoding="utf-8",
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_intent(config: dict[str, object], host_id: str, generation: int = 7) -> None:
    state = config["state"]
    assert isinstance(state, dict)
    Path(str(state["intent_path"])).parent.mkdir(parents=True, exist_ok=True)
    Path(str(state["intent_path"])).write_text(
        json.dumps({"schema_version": 1, "desired_host": host_id, "generation": generation, "advisory": True}),
        encoding="utf-8",
    )


def _write_journal(config: dict[str, object], value: dict[str, object]) -> Path:
    state = config["state"]
    assert isinstance(state, dict)
    path = Path(str(state["journal_path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, **value}), encoding="utf-8")
    return path


def _adapter(
    tmp_path: Path,
    config: dict[str, object],
    state: dict[str, object] | None = None,
    *,
    shared_state_path: Path | None = None,
) -> tuple[Path, Path]:
    host = config["host"]
    health = config["health"]
    tunnels = config["tunnels"]
    assert isinstance(host, dict) and isinstance(health, dict) and isinstance(tunnels, dict)
    state_path = shared_state_path or (tmp_path / f"adapter-state-{host['id']}.json")
    if state is not None or not state_path.exists():
        state_path.write_text(json.dumps(state or _default_state(config)), encoding="utf-8")
    adapter_path = tmp_path / f"adapter-{host['id']}.ps1"

    def ps(value: object) -> str:
        return str(value).replace("'", "''")

    adapter_path.write_text(
        f"""
$global:ColdStandbyStatePath = '{ps(state_path)}'
$global:ColdStandbyHostId = '{ps(host['id'])}'
$global:ColdStandbyPeerId = '{ps(host['peer_id'])}'
$global:ColdStandbyLocalUrl = '{ps(health['local_url'])}'
$global:ColdStandbyPeerUrl = '{ps(health['peer_url'])}'
$global:ColdStandbyStableUrl = '{ps(health['stable_url'])}'
$global:ColdStandbyOAuthUrl = '{ps(health['oauth_discovery_url'])}'
$global:ColdStandbyBaseUrl = 'https://exomem.example.test'
$global:ColdStandbyLocalTunnelName = '{ps(tunnels['local_name'])}'
$global:ColdStandbyLocalTunnelId = '{ps(tunnels['local_id'])}'
$global:ColdStandbyPeerTunnelName = '{ps(tunnels['peer_name'])}'
$global:ColdStandbyPeerTunnelId = '{ps(tunnels['peer_id'])}'
$global:ColdStandbyProbeIndexes = @{{}}

function Get-FakeState {{ Get-Content -Raw -LiteralPath $global:ColdStandbyStatePath | ConvertFrom-Json -AsHashtable }}
function Save-FakeState($state) {{ $state | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $global:ColdStandbyStatePath -Encoding utf8 }}
function Add-FakeTrace($name) {{ $state = Get-FakeState; $state.trace = @($state.trace) + $name; Save-FakeState $state }}
function Get-FakeProbe($url) {{
    $state = Get-FakeState
    if ($state.probe_sequences.ContainsKey($url)) {{
        $index = if ($global:ColdStandbyProbeIndexes.ContainsKey($url)) {{ [int]$global:ColdStandbyProbeIndexes[$url] }} else {{ 0 }}
        $sequence = @($state.probe_sequences[$url])
        $global:ColdStandbyProbeIndexes[$url] = $index + 1
        return $sequence[[Math]::Min($index, $sequence.Count - 1)]
    }}
    if ($state.probe_override.ContainsKey($url)) {{ return $state.probe_override[$url] }}
    if ($url -eq $global:ColdStandbyLocalUrl) {{
        $service = $state.services[$global:ColdStandbyHostId]
        return @{{ state = if ($service -eq 'running') {{ 'ready' }} else {{ 'inactive' }}; instance_id = $global:ColdStandbyHostId }}
    }}
    if ($url -eq $global:ColdStandbyPeerUrl) {{
        if ($state.ContainsKey('pre_activation_peer') -and $state.services[$global:ColdStandbyHostId] -eq 'stopped') {{ return $state.pre_activation_peer }}
        if ($state.ContainsKey('post_stop_peer') -and $state.services[$global:ColdStandbyHostId] -eq 'stopped') {{ return $state.post_stop_peer }}
        $service = $state.services[$global:ColdStandbyPeerId]
        return @{{ state = if ($service -eq 'running') {{ 'ready' }} else {{ 'inactive' }}; instance_id = $global:ColdStandbyPeerId }}
    }}
    if ($url -eq $global:ColdStandbyStableUrl) {{
        if ($state.ContainsKey('pre_activation_stable') -and $state.services[$global:ColdStandbyHostId] -eq 'stopped') {{ return $state.pre_activation_stable }}
        if ($state.ContainsKey('post_route_public') -and $state.route.tunnel_id -eq $global:ColdStandbyLocalTunnelId) {{ return $state.post_route_public }}
        $active = if ($state.route.tunnel_id -eq $global:ColdStandbyLocalTunnelId) {{ $global:ColdStandbyHostId }} else {{ $global:ColdStandbyPeerId }}
        $service = $state.services[$active]
        return @{{ state = if ($service -eq 'running') {{ 'ready' }} else {{ 'inactive' }}; instance_id = $active }}
    }}
    return @{{ state = 'unknown' }}
}}

return @{{
    Now = {{ [int64]1700000000000 }}
    Sleep = {{ param($seconds) }}
    GetService = {{ param($name) $state = Get-FakeState; @{{ state = $state.services[$global:ColdStandbyHostId] }} }}
    GetServiceEnvironment = {{ param($name) $state = Get-FakeState; $state.service_environments[$global:ColdStandbyHostId] }}
    StartService = {{ param($name) $state = Get-FakeState; Add-FakeTrace 'start-service'; if ($state.start_result -eq 'fail') {{ throw 'simulated start failure' }}; $state = Get-FakeState; $state.services[$global:ColdStandbyHostId] = 'running'; Save-FakeState $state; $true }}
    StopService = {{ param($name) $state = Get-FakeState; Add-FakeTrace 'stop-service'; if ($state.stop_result -eq 'fail') {{ throw 'simulated stop failure' }}; $state = Get-FakeState; $state.services[$global:ColdStandbyHostId] = 'stopped'; Save-FakeState $state; $true }}
    SetDemandStart = {{ param($name) Add-FakeTrace 'demand-start'; $true }}
    InspectDesktopTask = {{ param($command, $delay) @{{ state = 'needs_update' }} }}
    RegisterDesktopTask = {{ param($command, $delay) Add-FakeTrace 'register-task'; $true }}
    InspectTunnelConfig = {{ param($policy) $state = Get-FakeState; @{{ state = $state.tunnel_config }} }}
    InspectTunnelServiceBinding = {{ param($name, $policy) $state = Get-FakeState; @{{ state = $state.tunnel_service_binding }} }}
    PrepareTunnelConfig = {{ param($policy) Add-FakeTrace 'backup-tunnel-config'; Add-FakeTrace 'write-temp-tunnel-config'; Add-FakeTrace 'validate-tunnel-config'; Add-FakeTrace 'replace-tunnel-config'; $true }}
    ReloadTunnelService = {{ param($name) Add-FakeTrace 'reload-tunnel-service'; $true }}
    Probe = {{ param($url) Get-FakeProbe $url }}
    ProbeOAuth = {{ param($url) $state = Get-FakeState; if ($state.oauth_override.ContainsKey($url)) {{ return $state.oauth_override[$url] }}; @{{ state = 'ready'; issuer = $global:ColdStandbyBaseUrl }} }}
    GetSyncthing = {{ param($syncthing) $state = Get-FakeState; $state.syncthing }}
    GetMarkerVersion = {{ param($intentPath) $intent = Get-Content -Raw -LiteralPath $intentPath | ConvertFrom-Json -AsHashtable; @{{ generation = [int64]$intent.generation; version = "marker:$($intent.generation)" }} }}
    GetIntentDelivery = {{ param($syncthing, $generation, $intentPath) $state = Get-FakeState; $intent = Get-Content -Raw -LiteralPath $intentPath | ConvertFrom-Json -AsHashtable; @{{ state = $state.intent_delivery; generation = if ($state.intent_delivery -eq 'delivered' -and [int64]$intent.generation -eq [int64]$generation) {{ $generation }} else {{ $null }}; marker_version = if ($state.intent_delivery -eq 'delivered') {{ "marker:$generation" }} elseif ($state.intent_delivery -eq 'stale') {{ 'marker:stale' }} else {{ $null }} }} }}
    GetTunnel = {{ param($name) $state = Get-FakeState; @{{ state = $state.tunnels[$name] }} }}
    GetRoute = {{ param($cloudflare, $tunnels) $state = Get-FakeState; $state.route }}
    SetRoute = {{ param($tunnel, $hostname) Add-FakeTrace "set-route:$tunnel"; $state = Get-FakeState; if ($state.set_route -eq 'fail') {{ $state.set_route = 'ok'; Save-FakeState $state; throw 'simulated route failure' }}; if ($state.set_route -ne 'stale') {{ $id = if ($tunnel -eq $global:ColdStandbyLocalTunnelName -or $tunnel -eq $global:ColdStandbyLocalTunnelId) {{ $global:ColdStandbyLocalTunnelId }} else {{ $global:ColdStandbyPeerTunnelId }}; $state.route = @{{ state = 'known'; tunnel_id = $id; target = "$id.cfargotunnel.com" }}; Save-FakeState $state }}; $true }}
    WriteJson = {{ param($path, $value) Add-FakeTrace "write-json:$([IO.Path]::GetFileName($path))"; $parent = Split-Path -Parent $path; New-Item -ItemType Directory -Force -Path $parent | Out-Null; $value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $path -Encoding utf8; $true }}
    ReadJson = {{ param($path) if (Test-Path -LiteralPath $path) {{ Get-Content -Raw -LiteralPath $path | ConvertFrom-Json -AsHashtable }} else {{ $null }} }}
    GetTrace = {{ $state = Get-FakeState; @($state.trace) }}
}}
""",
        encoding="utf-8",
    )
    return adapter_path, state_path


def _run(config: Path, adapter: Path, *args: str, json_output: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "TEST_BASE_URL": "https://exomem.example.test/",
        "TEST_GITHUB_CLIENT_ID": "client-id",
        "TEST_GITHUB_CLIENT_SECRET": "client-secret",
        "TEST_GITHUB_USERNAME": "operator-login",
        "TEST_GITHUB_USER_ID": "123456",
        "TEST_JWT_SIGNING_KEY": "not-for-output",
        "TEST_SYNCTHING_API_KEY": "not-for-output-either",
        "TEST_CLOUDFLARE_API_TOKEN": "not-for-output-cloudflare",
        "TEST_CLOUDFLARE_ZONE_ID": "test-zone-id",
    }
    command = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *args, "-ConfigPath", str(config), "-AdapterPath", str(adapter)]
    if json_output:
        command.append("-Json")
    return subprocess.run(command, text=True, capture_output=True, check=False, env=env)


def _terminal(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:  # pragma: no cover - assertion context
        raise AssertionError((result.returncode, result.stdout, result.stderr)) from error


def _prepared(tmp_path: Path, *, role: str = "laptop", intent: str | None = None) -> tuple[Path, dict[str, object], Path, Path]:
    path, config = _config(tmp_path, role=role)
    _write_matching_manifests(config)
    if intent is not None:
        _write_intent(config, intent)
    adapter, state_path = _adapter(tmp_path, config)
    return path, config, adapter, state_path


def test_status_is_bounded_read_only_and_names_unknown_evidence(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    state = _default_state(config)
    health = config["health"]
    assert isinstance(health, dict)
    state["probe_override"] = {str(health["peer_url"]): {"state": "unreachable"}}
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Status")

    assert result.returncode == 0, (result.stderr, result.stdout)
    terminal = _terminal(result)
    assert terminal["action"] == "Status"
    assert terminal["active_host"] == "unknown"
    assert "peer health is unknown" in terminal["reasons"]
    assert terminal["next_action"] == "none: resolve unknown or unsafe evidence"
    assert terminal["evidence"]["peer_health"]["samples"] == 3
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_status_human_terminal_is_useful_and_redacted(tmp_path: Path) -> None:
    config_path, _, adapter, _ = _prepared(tmp_path)

    result = _run(config_path, adapter, "Status", json_output=False)

    assert result.returncode == 0
    assert "Status" in result.stdout
    assert "Next safe action" in result.stdout
    assert "not-for-output" not in result.stdout
    assert "operator-123" not in result.stdout


def test_status_reports_both_ready_as_unsafe_without_mutation(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state = _default_state(config)
    host = config["host"]
    assert isinstance(host, dict)
    state["services"] = {str(host["id"]): "running", str(host["peer_id"]): "running"}
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Status")

    terminal = _terminal(result)
    assert terminal["active_host"] == "unsafe_ambiguous"
    assert "both origins report ready" in terminal["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


@pytest.mark.parametrize(
    "damage, expected",
    [
        (("host", "peer_id", "laptop"), "host and peer identities must be distinct"),
        (("tunnels", "peer_name", "laptop-tunnel"), "local and peer tunnel names must be distinct"),
        (("tunnels", "peer_id", LAPTOP_TUNNEL_ID), "local and peer tunnel IDs must be distinct"),
        (("health", "peer_url", "https://laptop.ops.example.test/health/ready"), "local and peer health URLs must be distinct"),
        (("health", "peer_url", "https://exomem.example.test/health/ready"), "peer readiness URL must use peer_operational_hostname"),
    ],
)
def test_invalid_identity_configuration_refuses_before_mutation(tmp_path: Path, damage: tuple[str, str, str], expected: str) -> None:
    config_path, config = _config(tmp_path)
    section = config[damage[0]]
    assert isinstance(section, dict)
    section[damage[1]] = damage[2]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    adapter, state_path = _adapter(tmp_path, config)

    result = _run(config_path, adapter, "Configure")

    assert result.returncode != 0
    assert any(expected in item for item in _terminal(result)["reasons"])
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_missing_required_configuration_refuses_before_adapter_load(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    del config["syncthing"]["folder_id"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    adapter, state_path = _adapter(tmp_path, config)

    result = _run(config_path, adapter, "Configure")

    assert result.returncode != 0
    assert "Configuration requires 'syncthing.folder_id'." in _terminal(result)["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_status_bounds_malformed_replicated_generation(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path)
    state_config = config["state"]
    assert isinstance(state_config, dict)
    intent_path = Path(str(state_config["intent_path"]))
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(json.dumps({"desired_host": "laptop", "generation": "bad"}), encoding="utf-8")

    result = _run(config_path, adapter, "Status")

    assert result.returncode == 0, (result.stdout, result.stderr)
    terminal = _terminal(result)
    assert terminal["terminal"] == "status"
    assert "desired-host intent is unknown" in terminal["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_safety_evidence_urls_require_https(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    config["health"]["peer_url"] = "http://desktop.ops.example.test/health/ready"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    adapter, state_path = _adapter(tmp_path, config)

    result = _run(config_path, adapter, "Status")

    assert result.returncode != 0
    assert "must use HTTPS" in _terminal(result)["reasons"][0]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_configure_refuses_cloudflared_service_bound_to_another_config(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    state = _default_state(config)
    state["tunnel_service_binding"] = "wrong_config"
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Configure")

    assert result.returncode != 0
    assert "not bound to the configured file" in _terminal(result)["reasons"][0]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


@pytest.mark.parametrize("action", ["Configure", "Activate", "Handoff"])
def test_whatif_evaluates_preconditions_and_never_mutates(tmp_path: Path, action: str) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, intent="laptop")
    if action == "Handoff":
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["services"] = {"laptop": "running", "desktop": "stopped"}
        state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
        state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run(config_path, adapter, action, "-WhatIf")

    assert result.returncode == 0, (result.stdout, result.stderr)
    terminal = _terminal(result)
    assert terminal["terminal"] == "what_if"
    assert terminal["plan"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


@pytest.mark.parametrize("action, phase", [("Activate", "routing"), ("Handoff", "stopping_source")])
def test_whatif_never_replays_an_interrupted_operation(tmp_path: Path, action: str, phase: str) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, intent="laptop")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if action == "Handoff":
        state["services"] = {"laptop": "running", "desktop": "stopped"}
        state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
        desired_host = "desktop"
    else:
        desired_host = "laptop"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    journal_path = _write_journal(
        config,
        {
            "action": action,
            "phase": phase,
            "generation": 8,
            "desired_host": desired_host,
            "previous_route": DESKTOP_TUNNEL_ID if action == "Activate" else LAPTOP_TUNNEL_ID,
            "started_service": action == "Activate",
            "source_was_running": action == "Handoff",
            "marker_version": "marker:8",
            "terminal": None,
            "identity": {"expected_base_url": "https://exomem.example.test"},
        },
    )
    before_journal = journal_path.read_text(encoding="utf-8")

    _run(config_path, adapter, action, "-WhatIf")

    assert journal_path.read_text(encoding="utf-8") == before_journal
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_configure_is_explicit_idempotent_and_role_scoped(tmp_path: Path) -> None:
    desktop_path, desktop_config = _config(tmp_path, role="desktop")
    desktop_adapter, desktop_state_path = _adapter(tmp_path, desktop_config)

    result = _run(desktop_path, desktop_adapter, "Configure")

    assert result.returncode == 0, (result.stdout, result.stderr)
    terminal = _terminal(result)
    assert terminal["terminal"] == "configured"
    assert terminal["tunnel_policy"] == {
        "stable": "forward-all",
        "direct": ["/health", "/health/ready"],
        "direct_fallback": "http_status:404",
    }
    trace = json.loads(desktop_state_path.read_text(encoding="utf-8"))["trace"]
    assert trace == [
        "demand-start",
        "backup-tunnel-config",
        "write-temp-tunnel-config",
        "validate-tunnel-config",
        "replace-tunnel-config",
        "reload-tunnel-service",
        "write-json:manifest-desktop.json",
        "register-task",
    ]

    laptop_dir = tmp_path / "laptop"
    laptop_dir.mkdir()
    laptop_path, laptop_config = _config(laptop_dir, role="laptop")
    laptop_adapter, laptop_state_path = _adapter(laptop_dir, laptop_config)
    result = _run(laptop_path, laptop_adapter, "Configure")
    assert result.returncode == 0
    assert "register-task" not in json.loads(laptop_state_path.read_text(encoding="utf-8"))["trace"]


@pytest.mark.skipif(CLOUDFLARED is None, reason="cloudflared is not available")
def test_native_tunnel_preparation_is_atomic_idempotent_and_health_only(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    tunnels = config["tunnels"]
    assert isinstance(tunnels, dict)
    tunnel_config = Path(str(tunnels["config_path"]))
    tunnel_config.write_text(
        "\n".join(
            [
                f"tunnel: {tunnels['local_id']}",
                "credentials-file: operator-config/credentials.json",
                "ingress:",
                "  - hostname: unrelated.example.test",
                "    service: http://127.0.0.1:9999",
                "  - service: http_status:404",
                "",
            ]
        ),
        encoding="utf-8",
    )
    adapter = tmp_path / "native-tunnel-adapter.ps1"
    dotenv_path = str(config["identity_environment"]["dotenv_path"]).replace("'", "''")
    adapter.write_text(
        """
$native = Get-NativeAdapter
return @{
    SetDemandStart = { param($name) $true }
    GetServiceEnvironment = { param($name) @{ state = 'known'; values = Read-DotenvMap '__DOTENV_PATH__' } }
    InspectDesktopTask = { param($command, $delay) @{ state = 'not_applicable' } }
    RegisterDesktopTask = { param($command, $delay) throw 'not expected' }
    InspectTunnelConfig = $native.InspectTunnelConfig
    InspectTunnelServiceBinding = { param($name, $policy) @{ state = 'ready' } }
    PrepareTunnelConfig = $native.PrepareTunnelConfig
    ReloadTunnelService = { param($name) $true }
    GetTunnel = { param($name) @{ state = 'connected' } }
    WriteJson = $native.WriteJson
    GetTrace = { @() }
}
""".replace("__DOTENV_PATH__", dotenv_path),
        encoding="utf-8",
    )

    first = _run(config_path, adapter, "Configure")

    assert first.returncode == 0, (first.stdout, first.stderr)
    rendered = tunnel_config.read_text(encoding="utf-8")
    assert "hostname: unrelated.example.test" in rendered
    assert "hostname: exomem.example.test" in rendered
    assert "path: ^/health(?:/ready)?$" in rendered
    direct_index = rendered.index("hostname: laptop.ops.example.test")
    health_index = rendered.index("path: ^/health(?:/ready)?$", direct_index)
    direct_404_index = rendered.index("service: http_status:404", health_index)
    assert direct_index < health_index < direct_404_index
    assert rendered.rstrip().endswith("- service: http_status:404")
    backups = list(tmp_path.glob("cloudflared-laptop.yml.exomem-cold-standby.*.bak"))
    assert len(backups) == 1
    assert "hostname: unrelated.example.test" in backups[0].read_text(encoding="utf-8")

    second = _run(config_path, adapter, "Configure")

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert tunnel_config.read_text(encoding="utf-8") == rendered
    assert len(list(tmp_path.glob("cloudflared-laptop.yml.exomem-cold-standby.*.bak"))) == 1

    def matched_rule(url: str) -> str:
        result = subprocess.run(
            [CLOUDFLARED, "tunnel", "--config", str(tunnel_config), "ingress", "rule", url],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        return result.stdout

    assert "service: http://127.0.0.1:8765" in matched_rule("https://laptop.ops.example.test/health")
    assert "service: http://127.0.0.1:8765" in matched_rule("https://laptop.ops.example.test/health/ready")
    for path in ("/mcp", "/.well-known/oauth-authorization-server", "/api/write", "/artifacts/item"):
        assert "service: http_status:404" in matched_rule(f"https://laptop.ops.example.test{path}")
    assert "service: http://127.0.0.1:8765" in matched_rule("https://exomem.example.test/mcp")


def test_native_syncthing_delivery_accepts_documented_file_response(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    _write_intent(config, "laptop")
    state_config = config["state"]
    assert isinstance(state_config, dict)
    intent_path = Path(str(state_config["intent_path"]))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/rest/db/file":
                stat = intent_path.stat()
                seconds, nanoseconds = divmod(stat.st_mtime_ns, 1_000_000_000)
                nanoseconds = (nanoseconds // 100) * 100 + 37
                modified = f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds))}.{nanoseconds:09d}Z"
                entry = {
                    "deleted": False,
                    "ignored": False,
                    "invalid": False,
                    "modified": modified,
                    "name": ".exomem-state/desired-host.json",
                    "size": stat.st_size,
                    "version": ["TESTDEVICE:1700000000000"],
                }
                payload = {"availability": [], "global": entry, "local": entry}
            elif parsed.path == "/rest/db/completion":
                payload = {"completion": 100, "needBytes": 0, "needItems": 0, "remoteState": "valid"}
            elif parsed.path == "/rest/db/remoteneed":
                payload = {"files": [], "page": 1, "perpage": 100}
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config["syncthing"]["api_url"] = f"http://127.0.0.1:{server.server_port}"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        state = _default_state(config)
        state["services"] = {"laptop": "running", "desktop": "stopped"}
        state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
        fake_adapter, state_path = _adapter(tmp_path, config, state)
        wrapper = tmp_path / "native-syncthing-adapter.ps1"
        escaped_adapter = str(fake_adapter).replace("'", "''")
        wrapper.write_text(
            f"$fake = . '{escaped_adapter}'\n"
            "$native = Get-NativeAdapter\n"
            "$fake.GetMarkerVersion = $native.GetMarkerVersion\n"
            "$fake.GetIntentDelivery = $native.GetIntentDelivery\n"
            "return $fake\n",
            encoding="utf-8",
        )

        result = _run(config_path, wrapper, "Handoff")

        assert result.returncode == 0, (result.stdout, result.stderr)
        assert _terminal(result)["terminal"] == "handoff_pending_activation"
        journal = json.loads(Path(str(state_config["journal_path"])).read_text(encoding="utf-8"))
        assert journal["syncthing_version"] == "TESTDEVICE:1700000000000"
        assert json.loads(state_path.read_text(encoding="utf-8"))["services"]["laptop"] == "stopped"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("peer_ready", "peer is ready"),
        ("peer_unreachable", "peer inactivity is unproven"),
        ("stable_ready", "stable endpoint is already ready"),
        ("stable_unreachable", "stable endpoint state is ambiguous"),
        ("sync_pending", "Syncthing is not converged"),
        ("sync_unknown", "Syncthing API is unavailable"),
        ("intent_conflict", "desired-host intent names peer"),
        ("manifest_mismatch", "shared configuration fingerprint mismatch: jwt_signing_key"),
    ],
)
def test_activate_refuses_unsafe_evidence_before_service_or_dns_mutation(tmp_path: Path, condition: str, reason: str) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state = _default_state(config)
    health = config["health"]
    config_state = config["state"]
    assert isinstance(health, dict) and isinstance(config_state, dict)
    if condition == "peer_ready":
        state["probe_override"] = {str(health["peer_url"]): {"state": "ready", "instance_id": "desktop"}}
    elif condition == "peer_unreachable":
        state["probe_override"] = {str(health["peer_url"]): {"state": "unreachable"}}
    elif condition == "stable_ready":
        state["probe_override"] = {str(health["stable_url"]): {"state": "ready", "instance_id": "desktop"}}
    elif condition == "stable_unreachable":
        state["probe_override"] = {str(health["stable_url"]): {"state": "unreachable"}}
    elif condition == "sync_pending":
        state["syncthing"]["pending_bytes"] = 4
        state["syncthing"]["peer_complete"] = False
    elif condition == "sync_unknown":
        state["syncthing"]["api"] = "unreachable"
    elif condition == "intent_conflict":
        _write_intent(config, "desktop")
    else:
        peer_manifest = json.loads(Path(str(config_state["peer_manifest_path"])).read_text(encoding="utf-8"))
        peer_manifest["fingerprints"]["jwt_signing_key"] = "f" * 64
        Path(str(config_state["peer_manifest_path"])).write_text(json.dumps(peer_manifest), encoding="utf-8")
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode != 0
    terminal = _terminal(result)
    assert reason in terminal["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_activate_orders_transition_and_verifies_route_identity_and_oauth(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode == 0, (result.stdout, result.stderr)
    terminal = _terminal(result)
    assert terminal["terminal"] == "activated"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    intent_index = trace.index("write-json:desired-host.json")
    start_index = trace.index("start-service")
    route_index = trace.index("set-route:laptop-tunnel")
    assert intent_index < start_index < route_index
    assert trace[-1] == "write-json:journal-laptop.json"
    assert terminal["route_target"] == f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"
    assert "not-for-output" not in result.stdout
    assert "operator-123" not in result.stdout


def test_planned_handoff_target_can_activate_from_connected_tunnel_502s(tmp_path: Path) -> None:
    config_path, _, adapter, state_path = _prepared(tmp_path, intent="laptop")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    state["pre_activation_peer"] = {"state": "unserved", "status_code": 502}
    state["pre_activation_stable"] = {"state": "unserved", "status_code": 502}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run(config_path, adapter, "Activate")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _terminal(result)["terminal"] == "activated"


def test_activate_failure_before_routing_never_changes_dns(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state = _default_state(config)
    state["start_result"] = "fail"
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode != 0
    assert _terminal(result)["terminal"] == "rolled_back"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert "start-service" in trace
    assert not any(item.startswith("set-route:") for item in trace)


def test_activate_compensates_wrong_public_origin_to_known_route(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state = _default_state(config)
    health = config["health"]
    assert isinstance(health, dict)
    state["probe_override"] = {str(health["stable_url"]): {"state": "ready", "instance_id": "desktop"}}
    # The first stable checks must be inactive; the fake switches to this override only
    # after routing by using the dedicated post-route override understood by the adapter.
    state["probe_override"] = {}
    state["post_route_public"] = {"state": "ready", "instance_id": "desktop"}
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode != 0
    terminal = _terminal(result)
    assert terminal["terminal"] == "rolled_back"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert trace.index("set-route:laptop-tunnel") < trace.index(f"set-route:{DESKTOP_TUNNEL_ID}") < trace.index("stop-service")


def test_activate_unknown_prior_route_requires_operator_recovery(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state = _default_state(config)
    state["route"] = {"state": "unknown", "tunnel_id": None, "target": None}
    state["set_route"] = "stale"
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode != 0
    terminal = _terminal(result)
    assert terminal["terminal"] == "operator_recovery_required"
    assert "stop-service" in json.loads(state_path.read_text(encoding="utf-8"))["trace"]


def test_interrupted_route_replay_commits_only_when_route_and_origin_agree(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path)
    config_state = config["state"]
    assert isinstance(config_state, dict)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["services"] = {"laptop": "running", "desktop": "stopped"}
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    Path(str(config_state["journal_path"])).write_text(
        json.dumps({
            "schema_version": 1,
            "action": "Activate",
            "phase": "route_changed",
            "generation": 8,
            "desired_host": "laptop",
            "previous_route": DESKTOP_TUNNEL_ID,
            "started_service": True,
            "terminal": None,
            "bypassed_guards": [],
        }),
        encoding="utf-8",
    )

    result = _run(config_path, adapter, "Activate")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _terminal(result)["terminal"] == "recovered_activation"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert not any(item.startswith("set-route:") for item in trace)
    assert "start-service" not in trace


def test_completed_activation_replay_is_idempotent(tmp_path: Path) -> None:
    config_path, _, adapter, state_path = _prepared(tmp_path)
    first = _run(config_path, adapter, "Activate")
    assert first.returncode == 0, (first.stdout, first.stderr)
    first_trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]

    second = _run(config_path, adapter, "Activate")

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert _terminal(second)["terminal"] == "already_activated"
    second_trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert second_trace == first_trace


def test_force_is_two_part_narrow_and_audited(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    _write_intent(config, "desktop")
    state = _default_state(config)
    health = config["health"]
    assert isinstance(health, dict)
    state["probe_override"] = {str(health["peer_url"]): {"state": "unreachable"}}
    state["syncthing"]["pending_bytes"] = 12
    state["syncthing"]["peer_complete"] = False
    adapter, state_path = _adapter(tmp_path, config, state)

    missing_ack = _run(config_path, adapter, "Activate", "-Force")
    assert missing_ack.returncode != 0
    assert "exact acknowledgement" in _terminal(missing_ack)["reasons"][0]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []

    result = _run(config_path, adapter, "Activate", "-Force", "-Acknowledge", ACK)
    assert result.returncode == 0, (result.stdout, result.stderr)
    terminal = _terminal(result)
    assert terminal["bypassed_guards"] == [
        "Syncthing is not converged",
        "peer inactivity is unproven",
        "desired-host intent names peer",
    ]
    journal = json.loads(Path(str(config["state"]["journal_path"])).read_text(encoding="utf-8"))
    assert journal["bypassed_guards"] == terminal["bypassed_guards"]


def test_force_cannot_bypass_unknown_stable_route_or_identity_drift(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    health = config["health"]
    state_config = config["state"]
    assert isinstance(health, dict) and isinstance(state_config, dict)
    peer_manifest = json.loads(Path(str(state_config["peer_manifest_path"])).read_text(encoding="utf-8"))
    peer_manifest["fingerprints"]["github_oauth_callback"] = "0" * 64
    Path(str(state_config["peer_manifest_path"])).write_text(json.dumps(peer_manifest), encoding="utf-8")
    state = _default_state(config)
    state["probe_override"] = {str(health["stable_url"]): {"state": "unreachable"}}
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate", "-Force", "-Acknowledge", ACK)

    assert result.returncode != 0
    reasons = _terminal(result)["reasons"]
    assert "stable endpoint state is ambiguous" in reasons
    assert "shared configuration fingerprint mismatch: github_oauth_callback" in reasons
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_duplicate_local_manifest_cannot_stand_in_for_peer_parity(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state_config = config["state"]
    assert isinstance(state_config, dict)
    local_manifest = json.loads(Path(str(state_config["manifest_path"])).read_text(encoding="utf-8"))
    Path(str(state_config["peer_manifest_path"])).write_text(json.dumps(local_manifest), encoding="utf-8")
    adapter, state_path = _adapter(tmp_path, config)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode != 0
    assert "shared configuration peer manifest identifies the wrong host" in _terminal(result)["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_status_bounds_malformed_manifest_shape(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state_config = config["state"]
    assert isinstance(state_config, dict)
    Path(str(state_config["peer_manifest_path"])).write_text(
        json.dumps({"schema_version": 1, "host_id": "desktop", "fingerprints": "corrupt"}),
        encoding="utf-8",
    )
    adapter, state_path = _adapter(tmp_path, config)

    result = _run(config_path, adapter, "Status")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "shared configuration peer manifest is unavailable" in _terminal(result)["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_effective_service_secret_drift_refuses_activation(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    state = _default_state(config)
    state["service_environments"]["laptop"]["values"]["TEST_GITHUB_CLIENT_SECRET"] = "different-secret"
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate")

    assert result.returncode != 0
    assert "effective shared configuration is unavailable" in _terminal(result)["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_force_cannot_bypass_any_positive_peer_ready_sample(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    _write_intent(config, "laptop")
    health = config["health"]
    assert isinstance(health, dict)
    state = _default_state(config)
    state["probe_sequences"] = {
        str(health["peer_url"]): [
            {"state": "ready", "instance_id": "desktop"},
            {"state": "unserved", "status_code": 502},
            {"state": "unserved", "status_code": 502},
        ]
    }
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Activate", "-Force", "-Acknowledge", ACK)

    assert result.returncode != 0
    terminal = _terminal(result)
    assert "peer is ready" in terminal["reasons"]
    assert "peer is ready" not in terminal["bypassed_guards"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_if_unserved_requires_desktop_intent_and_conclusive_inactivity(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, role="desktop", intent="laptop")

    result = _run(config_path, adapter, "Activate", "-IfUnserved")

    assert result.returncode != 0
    assert "activate-if-unserved requires intent naming desktop" in _terminal(result)["reasons"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["trace"] == []


def test_handoff_requires_exact_intent_delivery_before_stopping_source(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, intent="laptop")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["services"] = {"laptop": "running", "desktop": "stopped"}
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    state["intent_delivery"] = "unknown"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run(config_path, adapter, "Handoff")

    assert result.returncode != 0
    terminal = _terminal(result)
    assert terminal["terminal"] == "intent_delivery_unproven"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert "stop-service" not in trace
    assert not any(item.startswith("set-route:") for item in trace)


def test_handoff_retries_the_same_timed_out_generation(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, intent="laptop")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["services"] = {"laptop": "running", "desktop": "stopped"}
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    state["intent_delivery"] = "unknown"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    first = _run(config_path, adapter, "Handoff")
    assert _terminal(first)["terminal"] == "intent_delivery_unproven"
    journal = json.loads(Path(str(config["state"]["journal_path"])).read_text(encoding="utf-8"))
    generation = journal["generation"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["intent_delivery"] = "delivered"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    second = _run(config_path, adapter, "Handoff")

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert _terminal(second)["terminal"] == "handoff_pending_activation"
    final_journal = json.loads(Path(str(config["state"]["journal_path"])).read_text(encoding="utf-8"))
    assert final_journal["generation"] == generation


@pytest.mark.parametrize("phase, route_at_target", [("stopping_source", False), ("routing", True)])
def test_handoff_replays_power_loss_windows_without_split_brain(tmp_path: Path, phase: str, route_at_target: bool) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path)
    _write_intent(config, "desktop", generation=8)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["services"] = {"laptop": "stopped", "desktop": "stopped"}
    route_id = DESKTOP_TUNNEL_ID if route_at_target else LAPTOP_TUNNEL_ID
    state["route"] = {"state": "known", "tunnel_id": route_id, "target": f"{route_id}.cfargotunnel.com"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _write_journal(
        config,
        {
            "action": "Handoff",
            "phase": phase,
            "generation": 8,
            "desired_host": "desktop",
            "previous_route": LAPTOP_TUNNEL_ID,
            "source_was_running": True,
            "marker_version": "marker:8",
            "terminal": None,
            "identity": {"expected_base_url": "https://exomem.example.test"},
        },
    )

    result = _run(config_path, adapter, "Handoff")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _terminal(result)["terminal"] == "handoff_pending_activation"
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert list(final_state["services"].values()).count("running") == 0
    if route_at_target:
        assert not any(item.startswith("set-route:") for item in final_state["trace"])
    else:
        assert "set-route:desktop-tunnel" in final_state["trace"]


def test_handoff_stops_source_before_route_and_never_starts_remote(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, intent="laptop")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["services"] = {"laptop": "running", "desktop": "stopped"}
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run(config_path, adapter, "Handoff")

    assert result.returncode == 0, (result.stdout, result.stderr)
    terminal = _terminal(result)
    assert terminal["terminal"] == "handoff_pending_activation"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert trace.index("stop-service") < trace.index("set-route:desktop-tunnel")
    assert "start-service" not in trace
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert list(final_state["services"].values()).count("running") == 0


def test_handoff_failure_restarts_source_only_when_target_is_proven_inactive(tmp_path: Path) -> None:
    config_path, config, adapter, state_path = _prepared(tmp_path, intent="laptop")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["services"] = {"laptop": "running", "desktop": "stopped"}
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    state["set_route"] = "fail"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run(config_path, adapter, "Handoff")

    assert result.returncode != 0
    assert _terminal(result)["terminal"] == "rolled_back"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert trace.index("stop-service") < trace.index("start-service")
    assert json.loads(state_path.read_text(encoding="utf-8"))["services"]["laptop"] == "running"


def test_handoff_failure_keeps_source_stopped_when_target_state_is_unknown(tmp_path: Path) -> None:
    config_path, config = _config(tmp_path)
    _write_matching_manifests(config)
    _write_intent(config, "laptop")
    state = _default_state(config)
    host = config["host"]
    health = config["health"]
    assert isinstance(host, dict) and isinstance(health, dict)
    state["services"] = {"laptop": "running", "desktop": "stopped"}
    state["route"] = {"state": "known", "tunnel_id": LAPTOP_TUNNEL_ID, "target": f"{LAPTOP_TUNNEL_ID}.cfargotunnel.com"}
    # Peer is initially inactive, then compensation sees unknown through the post-stop override.
    state["post_stop_peer"] = {"state": "unreachable"}
    state["set_route"] = "fail"
    adapter, state_path = _adapter(tmp_path, config, state)

    result = _run(config_path, adapter, "Handoff")

    assert result.returncode != 0
    assert _terminal(result)["terminal"] == "operator_recovery_required"
    trace = json.loads(state_path.read_text(encoding="utf-8"))["trace"]
    assert "start-service" not in trace
    assert json.loads(state_path.read_text(encoding="utf-8"))["services"]["laptop"] == "stopped"


def test_desktop_laptop_round_trip_commits_with_at_most_one_active_service(tmp_path: Path) -> None:
    desktop_path, desktop_config = _config(tmp_path, role="desktop")
    laptop_path, laptop_config = _config(tmp_path, role="laptop")
    _write_matching_manifests(desktop_config)
    _write_matching_manifests(laptop_config)
    _write_intent(desktop_config, "desktop")
    shared_state_path = tmp_path / "two-host-world.json"
    world = _default_state(desktop_config)
    world["services"] = {"desktop": "running", "laptop": "stopped"}
    world["route"] = {"state": "known", "tunnel_id": DESKTOP_TUNNEL_ID, "target": f"{DESKTOP_TUNNEL_ID}.cfargotunnel.com"}
    desktop_adapter, _ = _adapter(tmp_path, desktop_config, world, shared_state_path=shared_state_path)
    laptop_adapter, _ = _adapter(tmp_path, laptop_config, shared_state_path=shared_state_path)

    def assert_active(expected: str | None) -> None:
        shared = json.loads(shared_state_path.read_text(encoding="utf-8"))
        active = [host for host, service in shared["services"].items() if service == "running"]
        assert active == ([] if expected is None else [expected])

    assert_active("desktop")
    to_laptop = _run(desktop_path, desktop_adapter, "Handoff")
    assert to_laptop.returncode == 0, (to_laptop.stdout, to_laptop.stderr)
    assert _terminal(to_laptop)["terminal"] == "handoff_pending_activation"
    assert_active(None)

    laptop_activate = _run(laptop_path, laptop_adapter, "Activate")
    assert laptop_activate.returncode == 0, (laptop_activate.stdout, laptop_activate.stderr)
    assert_active("laptop")

    to_desktop = _run(laptop_path, laptop_adapter, "Handoff")
    assert to_desktop.returncode == 0, (to_desktop.stdout, to_desktop.stderr)
    assert _terminal(to_desktop)["terminal"] == "handoff_pending_activation"
    assert_active(None)

    desktop_activate = _run(desktop_path, desktop_adapter, "Activate", "-IfUnserved")
    assert desktop_activate.returncode == 0, (desktop_activate.stdout, desktop_activate.stderr)
    assert_active("desktop")
