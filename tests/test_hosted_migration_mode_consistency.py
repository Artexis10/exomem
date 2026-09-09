"""Every deployment-lock migration-mode enumeration agrees, and the cell chart deliberately does not.

A hosted migration mode is selectable only if the lock schema, the composer, the platform
template and the provisioner's own models all admit it. The cell chart is the one deliberate
divergence: `governance-v3-to-v4` runs in the coordinator's Job against a stopped cell, and the
cell chart has no governance behaviour at all, so it must keep refusing the mode.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

# The core CI shards run without the provisioner package; the hosted-infrastructure
# job installs it and exercises this invariant, as the sibling hosted tests do.
_config = pytest.importorskip("exomem_provisioner.config")
_lifecycle = pytest.importorskip("exomem_provisioner.lifecycle")
DeploymentRuntimeUpgrade = _config.DeploymentRuntimeUpgrade
SelectedDeploymentRuntime = _config.SelectedDeploymentRuntime
LifecycleConfig = _lifecycle.LifecycleConfig

ROOT = Path(__file__).resolve().parents[1]
LOCK_SCHEMA = ROOT / "infra/contracts/exomem-hosted-deployment-lock-v2.schema.json"
COMPOSER = ROOT / "infra/scripts/hosted_composition_lock.py"
PLATFORM_TEMPLATE = ROOT / "infra/helm/platform/templates/_runtime-release.tpl"
CELL_SCHEMA = ROOT / "infra/helm/cell/values.schema.json"

CELL_ONLY_EXCLUDES = "governance-v3-to-v4"


def _composer():
    spec = importlib.util.spec_from_file_location("hosted_composition_lock", COMPOSER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _template_modes() -> set[str]:
    text = PLATFORM_TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"has \$upgrade\.migrationMode \(list ([^)]*)\)", text)
    assert match is not None, "the platform template must enumerate migration modes"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _upgrade(mode: str) -> dict[str, str]:
    return {
        "compatibilityDigest": "9" * 64,
        "migrationMode": mode,
        "substrateConsumerCommit": "8" * 40,
        "substrateTrustSha256": "7" * 64,
    }


def test_every_deployment_lock_migration_mode_enumeration_is_the_same_set() -> None:
    canonical = set(get_args(DeploymentRuntimeUpgrade.model_fields["migrationMode"].annotation))
    assert CELL_ONLY_EXCLUDES in canonical

    assert set(get_args(SelectedDeploymentRuntime.model_fields["migrationMode"].annotation)) == (
        canonical
    )
    assert set(get_args(get_type_hints(LifecycleConfig)["migration_mode"])) == canonical

    schema = json.loads(LOCK_SCHEMA.read_text(encoding="utf-8"))
    assert set(schema["$defs"]["runtimeUpgrade"]["properties"]["migrationMode"]["enum"]) == (
        canonical
    )

    assert _template_modes() == canonical

    composer = _composer()
    for mode in canonical:
        assert composer._runtime_upgrade(_upgrade(mode))["migrationMode"] == mode
    with pytest.raises(composer.CompositionError):
        composer._runtime_upgrade(_upgrade("governance-v4-to-v5"))


def test_the_cell_chart_admits_every_mode_except_the_coordinator_owned_migration() -> None:
    canonical = set(get_args(DeploymentRuntimeUpgrade.model_fields["migrationMode"].annotation))
    cell = json.loads(CELL_SCHEMA.read_text(encoding="utf-8"))

    assert set(cell["properties"]["migrationMode"]["enum"]) == canonical - {CELL_ONLY_EXCLUDES}
