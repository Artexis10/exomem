"""Static checks for the published container runtime contract."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dockerfile_declares_cuda_target_as_capable_not_resident() -> None:
    text = _read("Dockerfile")

    assert "FROM builder-lean AS builder-cuda" in text
    assert "--index-url https://download.pytorch.org/whl/cu132" in text
    assert "FROM python:3.12-slim AS cuda" in text
    assert "CUDA-capable torch, CPU-default at idle" in text
    assert "EXOMEM_CONTAINER_VARIANT=cuda" in text
    assert "EXOMEM_MODE=performance" not in text


def test_dockerfile_has_a_fixed_nonroot_immutable_hosted_target() -> None:
    text = _read("Dockerfile")
    hosted = text.split("FROM python:3.12-slim AS hosted", 1)[1].split(
        "FROM python:3.12-slim AS lean", 1
    )[0]

    assert "ARG EXOMEM_RELEASE_BUILD_TIME" in hosted
    assert 'test -n "$EXOMEM_RELEASE_BUILD_TIME"' in hosted
    assert "EXOMEM_RELEASE_BUILD_TIME=${EXOMEM_RELEASE_BUILD_TIME}" in hosted
    assert "EXOMEM_CONTAINER_VARIANT=hosted" in hosted
    assert "PYTHONDONTWRITEBYTECODE=1" in hosted
    assert "USER 10001:10001" in hosted
    assert "ENTRYPOINT [\"exomem\"]" in hosted
    assert "VOLUME" not in hosted


def test_release_workflow_publishes_cuda_tags() -> None:
    text = _read(".github/workflows/release-please.yml")

    assert "target: cuda" in text
    assert "platforms: linux/amd64" in text
    assert "ghcr.io/artexis10/exomem:cuda" in text
    assert "ghcr.io/artexis10/exomem:${{ steps.meta.outputs.version }}-cuda" in text


def test_release_workflow_publishes_hosted_image_with_immutable_build_time() -> None:
    text = _read(".github/workflows/release-please.yml")

    assert text.count("target: hosted") == 2
    assert text.count("EXOMEM_RELEASE_BUILD_TIME=${{ steps.meta.outputs.build_time }}") == 2
    assert text.count(
        "ghcr.io/artexis10/exomem:${{ steps.meta.outputs.version }}-hosted"
    ) == 2
    assert "ghcr.io/artexis10/exomem:hosted" in text
    assert text.count('build_time=$(git show -s --format=%cI "$TAG")') == 2


def test_compose_overrides_select_cpu_ml_and_cuda() -> None:
    ml = _read("compose.ml.yaml")
    cuda = _read("compose.cuda.yaml")

    assert "image: ghcr.io/artexis10/exomem:ml" in ml
    assert "image: ghcr.io/artexis10/exomem:cuda" in cuda
    assert "gpus: all" in cuda
    assert "EXOMEM_MODE=performance" in cuda


def test_docker_docs_explain_cuda_capability_and_os_tradeoffs() -> None:
    text = _read("docs/docker.md")

    assert "`cuda`, `X.Y.Z-cuda`" in text
    assert "The CUDA image is **capable**, not **resident by default**" in text
    assert "native NSSM remains" in text
    assert "Docker Linux containers do not expose Metal/MPS/MLX" in text


def test_windows_service_scripts_gate_selected_profile_before_success() -> None:
    restart = _read("scripts/restart.ps1")
    install = _read("scripts/install-service.ps1")

    assert '[string]$Profile = "hybrid"' in restart
    assert "Invoke-DoctorGate -Profile $Profile" in restart
    assert restart.index("Invoke-DoctorGate -Profile $Profile") < restart.index('Write-Host "Stopping $ServiceName..."')

    assert '[string]$Profile = "standard"' in install
    assert '"-Profile", $Profile' in install
    assert "exomem\", \"doctor\", \"--profile\", $Profile" in install
    assert install.index("exomem\", \"doctor\", \"--profile\", $Profile") < install.index("& $NssmPath install")


def test_windows_service_installer_supports_release_env_and_cuda() -> None:
    install = _read("scripts/install-service.ps1")
    # The package-install and CUDA-repair steps live in the shared helper so that
    # upgrade.ps1 performs them identically; the installer delegates rather than
    # carrying its own copy.
    common = _read("scripts/_service-common.ps1")

    assert "[switch]$Release" in install
    assert "exomem-service-release" in install
    assert "Install-ExomemPackage" in install
    assert "Repair-TorchCuda" in install
    assert "AppEnvironmentExtra" in install
    assert "EXOMEM_MCP_LEGACY_COMPAT" in install
    assert "Test-McpEndpoint -HostName $BindHost -EndpointPort $Port" in install

    assert '"uv", "pip", "install", "--upgrade", "--python", $Python, $pkg' in common
    assert "https://download.pytorch.org/whl/cu132" in common


def test_cuda_repair_derives_the_torch_version_instead_of_pinning_one() -> None:
    """A hardcoded pin here goes stale and starts DOWNGRADING torch.

    The previous `torch==2.12.0+cu132` pin would have knocked a box already on
    2.13.0+cu132 backwards on every upgrade. The repair must reinstall the CUDA
    build of whatever version is already installed, and never substitute another.
    """
    common = _read("scripts/_service-common.ps1")

    assert "+cu132" in common
    assert '$target = "torch==$(($installed -split \'\\+\')[0])+cu132"' in common
    # No literal x.y.z pin may reappear.
    assert not re.search(r"torch==\d+\.\d+\.\d+", common)


def test_installer_reuses_the_venv_the_service_already_runs() -> None:
    """Re-running to upgrade must not silently provision a second venv.

    The default root is 'exomem-service-release', but a box may be installed
    anywhere (-ServiceRoot); the NSSM registry is the only source of truth.
    """
    install = _read("scripts/install-service.ps1")
    common = _read("scripts/_service-common.ps1")

    assert "Get-ExomemServiceRoot" in install
    assert "Parameters" in common and "Application" in common


def test_installer_is_idempotent_for_an_already_registered_service() -> None:
    """`nssm install` fails on an existing service, which made the documented
    upgrade path unsafe -- so nobody re-ran it and the service drifted."""
    install = _read("scripts/install-service.ps1")

    assert "$NssmPath set $ServiceName Application $python" in install
    assert "$NssmPath install $ServiceName $python" in install


def test_upgrade_script_verifies_the_live_version_after_restart() -> None:
    """Installing is not deploying: the running process must report the new
    version, or the restart silently came back on the old code."""
    upgrade = _read("scripts/upgrade.ps1")

    assert "Install-ExomemPackage" in upgrade
    assert "Repair-TorchCuda" in upgrade
    assert "/health" in upgrade
    assert "Version mismatch" in upgrade
