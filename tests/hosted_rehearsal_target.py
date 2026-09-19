"""Resolve the rehearsal's image and identities from the paired trust registry."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT = _ROOT / "infra/operations/hosted-runtime-0.77.0-20260910/target.json"
_FIELDS = {
    "releaseVersion", "sourceCommit", "runtimeImage", "runtimeCandidateSha256",
    "protocolVersion", "agentProfile", "gatewayContractDigest", "commandFingerprint",
    "schemaDigest", "compatibilityDigest",
}


def load_rehearsal_target() -> dict[str, str]:
    """Resolve before cluster effects; an override must exist in the consumer registry.

    This is selection of reviewed evidence, not a replacement provenance verifier.
    Adding a release to that registry still requires its signed release artifacts.
    """
    release = os.environ.get("EXOMEM_REHEARSAL_RELEASE", "0.77.0")
    companion = os.environ.get("SUBSTRATE_REHEARSAL_REPO")
    if companion is None:
        if release != "0.77.0":
            raise ValueError("a new rehearsal release requires the companion trust registry")
        target = json.loads(_DEFAULT.read_text())
    else:
        repo = Path(companion).resolve()
        script = repo / "scripts/hosted-cluster-rehearsal.ts"
        if not script.is_file():
            raise ValueError("companion runtime target helper is missing")
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("EXOMEM_") and key != "DATABASE_URL"
        }
        environment["EXOMEM_REHEARSAL_RELEASE"] = release
        result = subprocess.run(
            ["node", "--import", "tsx", str(script), "--describe-runtime-target"],
            cwd=repo, env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise ValueError("companion refused the reviewed runtime target; verify release adoption")
        target = json.loads(result.stdout)
    if not isinstance(target, dict) or set(target) != _FIELDS:
        raise ValueError("rehearsal target requires exactly the ten reviewed identity fields")
    if any(not isinstance(value, str) or not value for value in target.values()):
        raise ValueError("rehearsal target identity fields must be nonempty strings")
    if target["releaseVersion"] != release or not re.fullmatch(r"\d+\.\d+\.\d+", release):
        raise ValueError("rehearsal target release differs from selection")
    if not re.fullmatch(r"ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}", target["runtimeImage"]):
        raise ValueError("rehearsal target requires an immutable runtime image")
    if not re.fullmatch(r"[0-9a-f]{40}", target["sourceCommit"]):
        raise ValueError("rehearsal target requires an exact source commit")
    for key in ("runtimeCandidateSha256", "gatewayContractDigest", "commandFingerprint", "schemaDigest", "compatibilityDigest"):
        if not re.fullmatch(r"[0-9a-f]{64}", target[key]):
            raise ValueError("rehearsal target digest is invalid")
    return target
