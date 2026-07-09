"""Static checks for the published container runtime contract."""

from __future__ import annotations

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


def test_release_workflow_publishes_cuda_tags() -> None:
    text = _read(".github/workflows/release-please.yml")

    assert "target: cuda" in text
    assert "platforms: linux/amd64" in text
    assert "ghcr.io/artexis10/exomem:cuda" in text
    assert "ghcr.io/artexis10/exomem:${{ steps.meta.outputs.version }}-cuda" in text


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

    assert '[string]$Profile = "hybrid"' in install
    assert '"-Profile", $Profile' in install
    assert "exomem\", \"doctor\", \"--profile\", $Profile" in install
    assert install.index("exomem\", \"doctor\", \"--profile\", $Profile") < install.index("& $NssmPath install")


def test_windows_service_installer_supports_release_env_and_cuda() -> None:
    install = _read("scripts/install-service.ps1")

    assert "[switch]$Release" in install
    assert "exomem-service-release" in install
    assert '"pip", "install", "--python", $venvPython, $pkg' in install
    assert "AppEnvironmentExtra" in install
    assert "EXOMEM_MCP_LEGACY_COMPAT" in install
    assert "https://download.pytorch.org/whl/cu132" in install
    assert "torch==2.12.0+cu132" in install
