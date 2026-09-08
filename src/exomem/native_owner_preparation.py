"""Prepare custody migration in a separate process with prospective settings."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

from .governance import authorization_custody as custody
from .native_owner_reviews import _thaw
from .native_owner_setup import NativeOwnerSetup, NativeOwnerSetupUnavailable
from .vocabulary_placement import validated_authority_directory

_CONFIG = (
    custody.KEYRING_FILE_ENV,
    custody.CONTROL_FILE_ENV,
    custody.MEMBERSHIP_FILE_ENV,
    custody.REPLICA_ID_ENV,
)


def deployment_binding(vault_root: Path) -> tuple[dict, dict[str, str]]:
    from .native_owner_maintenance import (
        load_service_environment,
        service_binding,
        service_environment_digest,
    )

    root = Path(vault_root).absolute()
    raw_unit = os.environ.get("EXOMEM_OWNER_SERVICE_UNIT", "")
    if not raw_unit or not Path(raw_unit).is_absolute():
        raise NativeOwnerSetupUnavailable("managed owner service is not configured")
    unit = Path(raw_unit)
    service = service_binding(unit)
    environment = load_service_environment(unit)
    if (
        Path(environment.get("EXOMEM_VAULT_PATH", "")).absolute() != root
        or environment.get("EXOMEM_OWNER_SERVICE_UNIT") != raw_unit
        or environment.get("EXOMEM_VOCABULARY_AUTHORITY_DIR")
        != str(validated_authority_directory(root))
    ):
        raise NativeOwnerSetupUnavailable("managed service binding changed")
    return {
        "service_unit": str(unit),
        "service_interpreter": str(service.interpreter),
        "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
        "service_environment_digest": service_environment_digest(unit),
        "runtime_version": importlib.metadata.version("exomem"),
    }, environment


def prospective_custody(vault_root: Path, environment: dict[str, str]) -> dict[str, str]:
    configured = {name: environment.get(name, "") for name in _CONFIG}
    if any(configured.values()):
        if not all(configured.values()):
            raise NativeOwnerSetupUnavailable("custody configuration is incomplete")
        return configured
    parent = validated_authority_directory(vault_root) / "custody"
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.resolve(strict=True) != parent or not custody._private_parent_is_safe(parent):
        raise NativeOwnerSetupUnavailable("prospective custody directory is unsafe")
    return {
        custody.KEYRING_FILE_ENV: str(parent / "keyring.json"),
        custody.CONTROL_FILE_ENV: str(parent / "control.json"),
        custody.MEMBERSHIP_FILE_ENV: str(parent / "membership.json"),
        custody.REPLICA_ID_ENV: "native-"
        + hashlib.sha256(custody.standalone_attachment_id(vault_root).encode()).hexdigest()[:24],
    }


def migration_child(vault_root: Path, *, environment: dict[str, str], now: int, body=None) -> dict:
    request = {"vault_root": str(Path(vault_root).absolute()), "now": now, "body": _thaw(body)}
    child = subprocess.run(
        [sys.executable, "-m", "exomem.native_owner_preparation"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=60,
        env={**os.environ, **environment},
        check=False,
    )
    if child.returncode != 0 or len(child.stdout) > 128 * 1024:
        raise NativeOwnerSetupUnavailable("migration preparation could not establish a stable plan")
    try:
        result = json.loads(child.stdout)
        if not isinstance(result, dict) or set(result) != {
            "body",
            "display",
            "binding_digest",
            "expires_at",
        }:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise NativeOwnerSetupUnavailable from None


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError
        request = json.loads(raw)
        if set(request) != {"vault_root", "now", "body"}:
            raise ValueError
        setup = NativeOwnerSetup(Path(request["vault_root"]))
        if request["body"] is not None:
            # Rechecking must not refresh an accepted review's expiry.
            body = request["body"]
            from .governance.schema_migration import plan_summary

            plan = setup.recheck_migration(body, now=request["now"])
            display = plan_summary(plan)
            result = {
                "body": body,
                "display": display,
                "binding_digest": "",
                "expires_at": body["expires_at"],
            }
        else:
            prepared = setup.prepare_migration(now=request["now"])
            result = {
                "body": _thaw(prepared.body),
                "display": _thaw(prepared.display),
                "binding_digest": prepared.binding_digest,
                "expires_at": prepared.expires_at,
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - never print credentials or private child errors
        print("Owner migration preparation is unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
