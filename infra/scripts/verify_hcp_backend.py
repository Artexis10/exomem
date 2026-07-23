#!/usr/bin/env python3
"""Fail-closed HCP Terraform workspace preflight and live state proof."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

HCP_HOSTNAME = "app.terraform.io"
EXPECTED_TERRAFORM_VERSION = "1.15.8"
PROOF_WORKSPACE = "exomem-hosted-backend-proof"


class BackendProofError(RuntimeError):
    """Raised when HCP state authority or the proof fails closed."""


class HcpApiError(BackendProofError):
    """Raised for a content-free HCP API failure with a retryable status code."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorkspaceContract(NamedTuple):
    workspace_id: str
    name: str


class HcpClient:
    def __init__(self, *, token: str, hostname: str = HCP_HOSTNAME) -> None:
        if hostname != HCP_HOSTNAME:
            raise BackendProofError(f"unsupported HCP hostname: {hostname}")
        self._token = token
        self._base_url = f"https://{hostname}/api/v2"

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/vnd.api+json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise HcpApiError(
                f"HCP API {method} {path} failed with HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise BackendProofError(f"HCP API {method} {path} was unavailable") from exc
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BackendProofError("HCP API returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise BackendProofError("HCP API returned a non-object response")
        return parsed

    def workspace(self, organization: str, name: str) -> dict[str, Any]:
        org = urllib.parse.quote(organization, safe="")
        workspace = urllib.parse.quote(name, safe="")
        return self.request("GET", f"/organizations/{org}/workspaces/{workspace}")

    def current_state_version(self, workspace_id: str) -> dict[str, Any]:
        return self.request("GET", f"/workspaces/{workspace_id}/current-state-version")

    def lock(self, workspace_id: str, reason: str) -> None:
        self.request("POST", f"/workspaces/{workspace_id}/actions/lock", {"reason": reason})

    def unlock(self, workspace_id: str) -> None:
        self.request("POST", f"/workspaces/{workspace_id}/actions/unlock")

    def rollback(self, workspace_id: str, state_version_id: str) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/workspaces/{workspace_id}/state-versions",
            {
                "data": {
                    "type": "state-versions",
                    "relationships": {
                        "rollback-state-version": {
                            "data": {"type": "state-versions", "id": state_version_id}
                        }
                    },
                }
            },
        )


def _data_and_attributes(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = document.get("data")
    if not isinstance(data, dict):
        raise BackendProofError("HCP workspace response has no data object")
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise BackendProofError("HCP workspace response has no attributes object")
    return data, attributes


def validate_workspace(
    document: dict[str, Any],
    *,
    expected_name: str,
    expected_terraform_version: str,
    require_unlocked: bool,
) -> WorkspaceContract:
    data, attributes = _data_and_attributes(document)
    workspace_id = data.get("id")
    name = attributes.get("name")
    if not isinstance(workspace_id, str) or not workspace_id.startswith("ws-"):
        raise BackendProofError("HCP workspace response has an invalid ID")
    if name != expected_name:
        raise BackendProofError("HCP workspace name does not match the fixed root binding")
    if attributes.get("execution-mode") != "local":
        raise BackendProofError("HCP workspace execution mode must be local")
    setting_overwrites = attributes.get("setting-overwrites")
    if not isinstance(setting_overwrites, dict) or not setting_overwrites.get(
        "execution-mode"
    ):
        raise BackendProofError("HCP local execution mode must be an explicit workspace override")
    if attributes.get("vcs-repo") is not None:
        raise BackendProofError("HCP state-only workspace must not have a VCS attachment")
    if attributes.get("auto-apply") is not False:
        raise BackendProofError("HCP state-only workspace auto-apply must be disabled")
    if attributes.get("assessments-enabled") is not False:
        raise BackendProofError("HCP state-only workspace assessments must be disabled")
    if (
        attributes.get("global-remote-state") is not False
        or attributes.get("project-remote-state") is not False
    ):
        raise BackendProofError("HCP workspace remote-state sharing must be disabled")
    if attributes.get("terraform-version") != expected_terraform_version:
        raise BackendProofError("HCP workspace Terraform version does not match the pinned CLI")
    if require_unlocked and attributes.get("locked") is not False:
        raise BackendProofError("HCP workspace is locked")
    permissions = attributes.get("permissions")
    required_permissions = (
        "can-read-state-versions",
        "can-create-state-versions",
        "can-lock",
        "can-unlock",
    )
    if not isinstance(permissions, dict) or not all(
        permissions.get(permission) is True for permission in required_permissions
    ):
        raise BackendProofError("HCP token lacks required state and lock permissions")
    return WorkspaceContract(workspace_id=workspace_id, name=name)


def _required_environment() -> tuple[str, str]:
    organization = os.environ.get("TF_CLOUD_ORGANIZATION", "").strip()
    token = os.environ.get("TF_TOKEN_app_terraform_io", "").strip()
    if not organization:
        raise BackendProofError("TF_CLOUD_ORGANIZATION is required")
    if not token:
        raise BackendProofError("TF_TOKEN_app_terraform_io is required")
    return organization, token


def preflight_workspace(
    *, organization: str, token: str, workspace: str, require_unlocked: bool = True
) -> WorkspaceContract:
    client = HcpClient(token=token)
    return validate_workspace(
        client.workspace(organization, workspace),
        expected_name=workspace,
        expected_terraform_version=EXPECTED_TERRAFORM_VERSION,
        require_unlocked=require_unlocked,
    )


PROOF_CONFIGURATION = f'''terraform {{
  required_version = "= {EXPECTED_TERRAFORM_VERSION}"

  cloud {{
    workspaces {{
      name = "{PROOF_WORKSPACE}"
    }}
  }}
}}

variable "revision" {{
  type = string
}}

variable "hold_seconds" {{
  type = number
}}

variable "hold_marker" {{
  type = string
}}

resource "terraform_data" "proof" {{
  input            = var.revision
  triggers_replace = [var.revision]

  provisioner "local-exec" {{
    command = "umask 077; : > \"$HOLD_MARKER\"; sleep \"$HOLD_SECONDS\""
    environment = {{
      HOLD_MARKER  = var.hold_marker
      HOLD_SECONDS = tostring(var.hold_seconds)
    }}
  }}
}}

output "proof_revision" {{
  value = terraform_data.proof.input
}}
'''


def _terraform_environment(data_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("TF_WORKSPACE", None)
    environment.update(
        {
            "TF_DATA_DIR": str(data_dir),
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
        }
    )
    return environment


def _command(terraform_bin: str, directory: Path, *arguments: str) -> list[str]:
    return [terraform_bin, f"-chdir={directory}", *arguments]


def _run_checked(
    terraform_bin: str,
    directory: Path,
    environment: dict[str, str],
    *arguments: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        _command(terraform_bin, directory, *arguments),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        operation = arguments[0] if arguments else "command"
        raise BackendProofError(f"Terraform {operation} failed during HCP backend proof")
    return result


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=10)


def _unlock_with_retry(client: HcpClient, workspace_id: str) -> None:
    for attempt in range(5):
        try:
            client.unlock(workspace_id)
            return
        except HcpApiError as exc:
            if exc.status_code != 503 or attempt == 4:
                raise
            time.sleep(attempt + 1)


def _state_version_id(document: dict[str, Any]) -> str:
    data = document.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise BackendProofError("HCP current state version response has no ID")
    return data["id"]


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_live_proof(*, evidence_path: Path, hold_seconds: int) -> None:
    organization, token = _required_environment()
    workspace = preflight_workspace(
        organization=organization,
        token=token,
        workspace=PROOF_WORKSPACE,
        require_unlocked=True,
    )
    terraform_bin = os.environ.get("TERRAFORM_BIN", "terraform")
    resolved = shutil.which(terraform_bin)
    if resolved is None:
        raise BackendProofError("TERRAFORM_BIN does not resolve to an executable")
    client = HcpClient(token=token)

    with tempfile.TemporaryDirectory(prefix="exomem-hcp-proof-") as temporary_root:
        root = Path(temporary_root)
        first_root = root / "first"
        second_root = root / "second"
        first_root.mkdir(mode=0o700)
        second_root.mkdir(mode=0o700)
        (first_root / "main.tf").write_text(PROOF_CONFIGURATION, encoding="utf-8")
        (second_root / "main.tf").write_text(PROOF_CONFIGURATION, encoding="utf-8")
        first_env = _terraform_environment(first_root / ".terraform-data")
        second_env = _terraform_environment(second_root / ".terraform-data")
        _run_checked(resolved, first_root, first_env, "init", "-input=false")
        _run_checked(resolved, second_root, second_env, "init", "-input=false")

        baseline_marker = root / "baseline.marker"
        _run_checked(
            resolved,
            first_root,
            first_env,
            "apply",
            "-input=false",
            "-auto-approve",
            "-lock-timeout=0s",
            "-var=revision=baseline",
            "-var=hold_seconds=0",
            f"-var=hold_marker={baseline_marker}",
        )
        baseline_version = _state_version_id(client.current_state_version(workspace.workspace_id))

        lock_marker = root / "lock-held.marker"
        first = subprocess.Popen(
            _command(
                resolved,
                first_root,
                "apply",
                "-input=false",
                "-auto-approve",
                "-lock-timeout=0s",
                "-var=revision=lock-holder",
                f"-var=hold_seconds={hold_seconds}",
                f"-var=hold_marker={lock_marker}",
            ),
            env=first_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        first_finished_cleanly = False
        try:
            deadline = time.monotonic() + min(hold_seconds, 20)
            while (
                not lock_marker.exists()
                and first.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
            if not lock_marker.exists():
                raise BackendProofError("lock-holder apply did not reach its guarded hold point")

            contender = subprocess.run(
                _command(
                    resolved,
                    second_root,
                    "apply",
                    "-input=false",
                    "-auto-approve",
                    "-lock-timeout=0s",
                    "-var=revision=contender",
                    "-var=hold_seconds=0",
                    f"-var=hold_marker={root / 'contender.marker'}",
                ),
                env=second_env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            contender_output = f"{contender.stdout}\n{contender.stderr}".lower()
            if contender.returncode == 0 or "lock" not in contender_output:
                raise BackendProofError(
                    "concurrent Terraform writer was not rejected by HCP locking"
                )

            first.communicate(timeout=hold_seconds + 60)
            if first.returncode != 0:
                raise BackendProofError("lock-holder apply failed")
            first_finished_cleanly = True
        finally:
            if not first_finished_cleanly:
                _stop_process_group(first)

        locked_version = _state_version_id(client.current_state_version(workspace.workspace_id))
        if locked_version == baseline_version:
            raise BackendProofError("lock-holder apply did not publish a new state version")

        client.lock(workspace.workspace_id, "Exomem backend recovery proof")
        rollback = client.rollback(workspace.workspace_id, baseline_version)
        rollback_version = _state_version_id(rollback)
        current_version = _state_version_id(
            client.current_state_version(workspace.workspace_id)
        )
        if current_version != rollback_version:
            raise BackendProofError("rolled-back state version did not become current")

        recovered = _run_checked(
            resolved,
            first_root,
            first_env,
            "output",
            "-raw",
            "proof_revision",
        ).stdout.strip()
        if recovered != "baseline":
            raise BackendProofError(
                "historical state rollback did not restore the baseline revision; "
                "the proof workspace remains locked"
            )
        _run_checked(
            resolved,
            first_root,
            first_env,
            "plan",
            "-refresh-only",
            "-lock=false",
            "-detailed-exitcode",
            "-input=false",
            "-var=revision=baseline",
            "-var=hold_seconds=0",
            f"-var=hold_marker={root / 'refresh.marker'}",
        )
        _unlock_with_retry(client, workspace.workspace_id)

        _run_checked(
            resolved,
            first_root,
            first_env,
            "destroy",
            "-input=false",
            "-auto-approve",
            "-lock-timeout=0s",
            "-var=revision=baseline",
            "-var=hold_seconds=0",
            f"-var=hold_marker={root / 'cleanup.marker'}",
        )
        _write_evidence(
            evidence_path,
            {
                "schemaVersion": 1,
                "recordedAt": datetime.now(UTC).isoformat(),
                "organization": organization,
                "workspace": workspace.name,
                "workspaceId": workspace.workspace_id,
                "terraformVersion": EXPECTED_TERRAFORM_VERSION,
                "executionMode": "local",
                "concurrentWriterRejected": True,
                "baselineStateVersionId": baseline_version,
                "contendedStateVersionId": locked_version,
                "rollbackStateVersionId": rollback_version,
                "recoveredRevision": recovered,
                "proofWorkspaceCleaned": True,
            },
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    preflight = subcommands.add_parser("preflight", help="validate one fixed HCP workspace")
    preflight.add_argument("--workspace", required=True)
    proof = subcommands.add_parser("prove", help="run the live lock and rollback proof")
    proof.add_argument("--evidence", required=True, type=Path)
    proof.add_argument("--hold-seconds", type=int, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        organization, token = _required_environment()
        if arguments.command == "preflight":
            preflight_workspace(
                organization=organization,
                token=token,
                workspace=arguments.workspace,
                require_unlocked=True,
            )
        else:
            if arguments.hold_seconds < 5 or arguments.hold_seconds > 60:
                raise BackendProofError("--hold-seconds must be between 5 and 60")
            run_live_proof(
                evidence_path=arguments.evidence,
                hold_seconds=arguments.hold_seconds,
            )
    except BackendProofError as exc:
        print(f"HCP backend proof blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
