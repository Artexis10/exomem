"""Static checks for the published container runtime contract."""

from __future__ import annotations

import re
from pathlib import Path
import pytest

pytestmark = pytest.mark.reads_repository_docs

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _workflow_job(text: str, name: str, next_name: str | None = None) -> str:
    body = text.split(f"\n  {name}:\n", 1)[1]
    if next_name is not None:
        body = body.split(f"\n  {next_name}:\n", 1)[0]
    return body


def test_dockerfile_declares_cuda_target_as_capable_not_resident() -> None:
    text = _read("Dockerfile")

    assert "FROM builder-lean AS builder-cuda" in text
    assert "--index-url https://download.pytorch.org/whl/cu132" in text
    assert "FROM python:3.12-slim AS cuda" in text
    assert "CUDA-capable torch, CPU-default at idle" in text
    assert "EXOMEM_CONTAINER_VARIANT=cuda" in text
    assert "EXOMEM_MODE=performance" not in text


def test_dockerfile_has_a_fixed_nonroot_immutable_cloud_target() -> None:
    text = _read("Dockerfile")
    cell = text.split("FROM python:3.12-slim AS cell-runtime", 1)[1].split(
        "FROM python:3.12-slim AS lean", 1
    )[0]

    assert "ARG EXOMEM_RELEASE_BUILD_TIME" in cell
    assert 'test -n "$EXOMEM_RELEASE_BUILD_TIME"' in cell
    assert "EXOMEM_RELEASE_BUILD_TIME=${EXOMEM_RELEASE_BUILD_TIME}" in cell
    assert "EXOMEM_CONTAINER_VARIANT=cloud" in cell
    assert "PYTHONDONTWRITEBYTECODE=1" in cell
    assert "USER 10001:10001" in cell
    assert "ENTRYPOINT [\"exomem\"]" in cell
    assert "VOLUME" not in cell


def test_cell_images_bake_and_serve_the_model_their_cells_encode_with() -> None:
    """A cell can fetch nothing under its no-egress policy and read-only root,
    so the cloud image carries and names exactly the model its cells resolve:
    bge-m3, which a personal server also runs. The model's build stage fetches
    it and loads it offline at its declared width."""
    from exomem import recall_space

    cloud_model = recall_space.configured_recall_model({"EXOMEM_CLOUD_CELL": "1"})
    assert cloud_model == recall_space.PERSONAL_MODEL
    text = _read("Dockerfile")

    def stage(header: str) -> str:
        return text.split(header, 1)[1].split("\nFROM ", 1)[0]

    cloud_builder = stage("FROM builder-hosted AS builder-cloud-model")
    cloud = stage("FROM cell-runtime AS cloud")

    for part in (cloud_builder, cloud):
        assert f"{recall_space.RECALL_MODEL_ENV}={cloud_model}" in part
    assert "HF_HUB_OFFLINE=1" in cloud_builder
    assert "recall_space.declared_dim(MODEL_NAME)" in cloud_builder
    # The cloud builder fetches into a root of its own, so the English model
    # it was built over never reaches the cloud image.
    assert "HF_HOME=/opt/exomem-cloud-models" in cloud_builder
    assert "COPY --from=builder-cloud-model /opt/exomem-cloud-models /opt/exomem-models" in cloud
    assert "COPY --from=builder-hosted /opt/exomem-models" not in cloud


def test_dockerfile_cloud_target_sets_pod_local_log_dir_and_disables_fastmcp_egress() -> None:
    """D1.2 "Log directory" and D2: the cloud stage's own defaults, not just the
    manifest, must keep runtime logs off the tenant volume and avoid FastMCP's
    startup update check stalling every cold start on a no-egress NetworkPolicy."""
    text = _read("Dockerfile")
    cloud = text.split("FROM cell-runtime AS cloud", 1)[1].split(
        "FROM python:3.12-slim AS lean", 1
    )[0]

    assert "EXOMEM_LOG_DIR=/tmp/exomem-logs" in cloud
    assert "FASTMCP_CHECK_FOR_UPDATES=off" in cloud
    assert "FASTMCP_SHOW_SERVER_BANNER=false" in cloud


def test_release_workflow_publishes_cuda_tags() -> None:
    text = _read(".github/workflows/release-please.yml")

    assert "target: cuda" in text
    assert "platforms: linux/amd64" in text
    assert '"cuda ghcr.io/artexis10/exomem:$version-cuda cuda"' in text


def test_release_builds_cloud_images_natively_without_discovery_tags() -> None:
    import yaml

    jobs = yaml.safe_load(_read(".github/workflows/release-please.yml"))["jobs"]
    native = jobs["build-cloud-images"]
    assert native["strategy"]["fail-fast"] is False
    assert native["strategy"]["matrix"]["include"] == [
        {"arch": "amd64", "runner": "ubuntu-latest"},
        {"arch": "arm64", "runner": "ubuntu-24.04-arm"},
    ]
    builds = [
        step
        for step in native["steps"]
        if step.get("uses", "").startswith("docker/build-push-action")
    ]
    assert {step["id"] for step in builds} == {"cloud-build", "cellctl-build"}
    for step in builds:
        role = step["id"].removesuffix("-build")
        assert f"fromJSON(env.IMAGES).{role}.publish" in step["if"]
        assert "!cancelled()" in step["if"]
        assert "steps.recover.outcome == 'success'" in step["if"]
        assert step["with"]["platforms"] == "linux/${{ matrix.arch }}"
        assert step["with"]["provenance"] == "mode=min,version=v0.2"
        assert "push-by-digest=true" in step["with"]["outputs"]
        assert "env.DRY_RUN != 'true'" in step["with"]["outputs"]
        assert "tags" not in step["with"]
        assert "matrix.arch" in step["with"]["cache-from"]
        assert "matrix.arch" in step["with"]["cache-to"]
    writes = [
        step
        for step in native["steps"]
        if step.get("uses", "").startswith(("docker/login-action", "actions/upload-artifact"))
    ]
    assert all(
        "DRY_RUN != 'true'" in step.get("if", "")
        or "steps.digests.outcome == 'success'" in step.get("if", "")
        for step in writes
    )
    digests = next(step for step in native["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert digests["with"]["overwrite"] is True
    assert "github.run_id" in digests["with"]["name"]
    assert "github.run_attempt" not in digests["with"]["name"]


def test_serialized_publisher_joins_and_attests_both_cloud_roles_after_recovery() -> None:
    import yaml

    jobs = yaml.safe_load(_read(".github/workflows/release-please.yml"))["jobs"]
    publisher = jobs["publish-image"]
    assert "build-cloud-images" in publisher["needs"]
    assert "needs.build-cloud-images.result == 'success'" not in publisher["if"]
    assert publisher["concurrency"]["cancel-in-progress"] is False
    assert "release-image-publication" in publisher["concurrency"]["group"]
    steps = {step["id"]: step for step in publisher["steps"] if "id" in step}
    assert "release_recovery.py images" in steps["recover"]["run"]
    for role in ("cloud", "cellctl"):
        join = steps[f"{role}-build"]
        assert f'join-image --tag "$TAG" --image {role}' in join["run"]
        assert "env.DRY_RUN != 'true'" in join["if"]
        attestation = steps[f"{role}-attestation"]
        assert f"steps.{role}-build.outcome == 'success'" in attestation["if"]
        assert (
            attestation["with"]["subject-digest"] == f"${{{{ steps.{role}-build.outputs.digest }}}}"
        )
        assert any(
            f"release-image-evidence-{role}-" in step.get("with", {}).get("name", "")
            for step in publisher["steps"]
        )
    finalizer = publisher["steps"][-1]
    assert "always()" in finalizer["if"] and "!cancelled()" in finalizer["if"]
    assert "env.DRY_RUN != 'true'" in finalizer["if"]
    assert "release_recovery.py finish-images" in finalizer["run"]


def test_release_publish_runs_only_on_the_tag_commit() -> None:
    """Attestations name the run's ref and commit, so only a run on the release tag,
    at the tag's commit, may publish; every other run hands the publish to one."""
    text = _read(".github/workflows/release-please.yml")
    plan = _workflow_job(text, "plan", "build-artifacts")
    images = _workflow_job(text, "publish-image")

    assert '[[ ! "$tag" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+' in plan
    assert '"$api/releases/tags/$tag"' in plan
    assert 'source_commit=$(git rev-parse "refs/tags/$tag^{commit}")' in plan
    assert 'elif [[ "$GITHUB_REF" == "refs/tags/$tag" ]]; then' in plan
    assert 'if [[ "$GITHUB_SHA" != "$source_commit" ]]; then' in plan
    assert '"$api/actions/workflows/$workflow/dispatches"' in plan
    assert 'test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"' in images
    assert 'test "$GITHUB_REF" = "refs/tags/$TAG"' in images
    assert 'test "$GITHUB_SHA" = "$SOURCE_COMMIT"' in images


def test_release_publication_jobs_checkout_the_created_tag() -> None:
    text = _read(".github/workflows/release-please.yml")
    jobs = (
        _workflow_job(text, "build-artifacts", "publish-pypi"),
        _workflow_job(text, "publish-mcp-registry", "publish-image"),
        _workflow_job(text, "publish-image"),
    )

    for job in jobs:
        assert "ref: refs/tags/${{ needs.plan.outputs.tag }}" in job
        assert 'test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"' in job
        assert "ref: main" not in job
    # PyPI receives the wheel and sdist that build-artifacts built from the tag.
    assert "name: python-distributions" in _workflow_job(text, "publish-pypi", "publish-mcp-registry")


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

    # `--refresh-package exomem` is not decoration: uv serves the package index
    # from its HTTP cache, so an unpinned `--upgrade` can resolve back to the
    # installed release and exit 0 having done nothing (#578).
    assert '"uv", "pip", "install", "--upgrade", "--refresh-package", "exomem", "--python", $Python, $pkg' in common
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


# ---- macOS/Linux deploy parity ----
#
# The one-command upgrade shipped Windows-first. These lock the same two
# protections onto the unix path: gate the venv the service actually runs, and
# assert the LIVE version after restart.


def test_unix_upgrade_script_exists_and_verifies_the_live_version() -> None:
    upgrade = _read("scripts/upgrade.sh")

    assert "exomem_service_python" in upgrade
    assert "doctor --profile" in upgrade
    assert "/health" in upgrade
    assert "version mismatch" in upgrade
    assert 'exomem_stop_service "$SERVICE_ID"' in upgrade
    assert 'exomem_start_service "$UNIT_FILE" "$SERVICE_ID"' in upgrade
    assert 'exomem_assert_service_restarted "$WORKER_BEFORE" "$WORKER_AFTER"' in upgrade

    # Both service managers remain behind the selected-identity helper.
    common = _read("scripts/_service-common.sh")
    assert 'launchctl kickstart "gui/$(id -u)/$service_id"' in common
    assert 'systemctl --user start "$service_id"' in common


def test_unix_restart_gates_doctor_on_the_service_venv() -> None:
    """restart.sh used to restart blind - no preflight at all."""
    restart = _read("scripts/restart.sh")

    assert "exomem_service_python" in restart
    assert "doctor --profile" in restart
    assert "service NOT restarted" in restart
    # Must not gate the repo venv: a release install never runs it. Checked against
    # code lines only - the comment above that line legitimately names it.
    code = [ln for ln in restart.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "REPO_ROOT/.venv" in ln]


def test_unix_helper_reads_the_installed_unit_not_the_repo_layout() -> None:
    """The rendered plist/unit is the source of truth, mirroring the NSSM
    registry on Windows: the service root is wherever install time put it."""
    common = _read("scripts/_service-common.sh")

    assert "LaunchAgents" in common          # macOS plist
    assert "systemd/user" in common          # Linux unit
    assert "ExecStart=" in common
    assert "ProgramArguments" in common or "/bin/python" in common
    assert "8765" in common                  # documented port default


def test_unix_upgrade_documents_why_it_skips_the_cuda_repair() -> None:
    """Not an omission: PyPI's Linux torch is already CUDA-enabled and macOS uses
    Metal, so the Windows-only repair would be wrong here."""
    upgrade = _read("scripts/upgrade.sh")

    assert "cu132" not in upgrade
    assert "CUDA" in upgrade and "Windows" in upgrade


def test_cellctl_dockerfile_is_digest_pinned_nonroot_and_frozen() -> None:
    dockerfile = _read("infra/cellctl/Dockerfile")

    assert re.search(r"^ARG PYTHON_IMAGE=python:3\.12-slim@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert [line for line in dockerfile.splitlines() if line.startswith("FROM ")] == [
        "FROM ${PYTHON_IMAGE} AS build",
        "FROM ${PYTHON_IMAGE}",
    ]
    assert "--require-hashes" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project --compile-bytecode" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert 'ENTRYPOINT ["python3", "-m", "cellctl.main"]' in dockerfile
    # Nothing is built from an unpinned build backend.
    assert "--no-editable" not in dockerfile and "hatchling" not in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
