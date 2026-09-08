from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import held_fs

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ("owner-setup.sh", "_service-common.sh", "service-transition-receipt.py")


def runner_module():
    from exomem import native_owner_maintenance_runner

    return native_owner_maintenance_runner


def test_source_runner_forwards_exact_arguments_without_shell_interpolation(monkeypatch):
    runner = runner_module()
    calls = []
    monkeypatch.setattr(runner, "require_supported_platform", lambda: None)
    monkeypatch.setattr(runner.shutil, "which", lambda executable, **_kwargs: "/usr/bin/bash")
    monkeypatch.setattr(
        runner.os, "execv", lambda executable, args: calls.append((executable, args))
    )
    args = [
        "--unit-file",
        "/tmp/unit with spaces;$(touch never).service",
        "--request-id",
        "review",
        "--resume",
    ]
    runner.main(args)
    assert calls == [
        ("/usr/bin/bash", ["/usr/bin/bash", str(ROOT / "scripts" / "owner-setup.sh"), *args])
    ]


def test_runner_prefers_packaged_helpers_and_preserves_sys_argv(tmp_path, monkeypatch):
    runner = runner_module()
    monkeypatch.setattr(runner, "require_supported_platform", lambda: None)
    package = tmp_path / "exomem"
    service = package / "_service"
    service.mkdir(parents=True)
    for name in HELPERS:
        (service / name).write_text("fixture")
    monkeypatch.setattr(runner, "__file__", str(package / "native_owner_maintenance_runner.py"))
    monkeypatch.setattr(runner.sys, "argv", ["module", "--help"])
    monkeypatch.setattr(runner.shutil, "which", lambda executable, **_kwargs: "/bin/bash")
    calls = []
    monkeypatch.setattr(runner.os, "execv", lambda executable, args: calls.append(args))
    runner.main()
    assert calls == [["/bin/bash", str(service / "owner-setup.sh"), "--help"]]


@pytest.mark.parametrize("missing", ["platform", "bash", "scripts", "dependency"])
def test_runner_refuses_unsupported_or_incomplete_installation(tmp_path, monkeypatch, missing):
    runner = runner_module()
    if missing == "platform":
        monkeypatch.setattr(
            held_fs,
            "platform_support",
            lambda: held_fs.PlatformSupport(False, "fixture backend is unavailable"),
        )
    else:
        monkeypatch.setattr(runner, "require_supported_platform", lambda: None)
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda executable, **_kwargs: None if missing == "bash" else "/bin/bash",
    )
    if missing in {"scripts", "dependency"}:
        package = tmp_path / "exomem"
        package.mkdir()
        monkeypatch.setattr(runner, "__file__", str(package / "native_owner_maintenance_runner.py"))
        if missing == "dependency":
            (package / "_service").mkdir()
            (package / "_service" / "owner-setup.sh").write_text("fixture")
    with pytest.raises(SystemExit):
        runner.main(["--help"])


def test_runner_refuses_canonical_unsupported_host_before_helper_discovery(
    monkeypatch,
) -> None:
    runner = runner_module()
    reason = "fixture held-filesystem backend is unavailable"
    monkeypatch.setattr(held_fs, "platform_support", lambda: held_fs.PlatformSupport(False, reason))

    def unexpected(*_args, **_kwargs):
        pytest.fail("unsupported maintenance reached helper discovery or execution")

    monkeypatch.setattr(runner, "_script_path", unexpected)
    monkeypatch.setattr(runner.shutil, "which", unexpected)
    monkeypatch.setattr(runner.os, "execv", unexpected)

    with pytest.raises(SystemExit, match=reason):
        runner.main(["--unit-file", "/missing.service", "--request-id", "review"])


def test_runner_refuses_windows_even_with_held_filesystem_support(monkeypatch) -> None:
    runner = runner_module()
    monkeypatch.setattr(held_fs, "platform_support", lambda: held_fs.PlatformSupport(True))
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))

    with pytest.raises(SystemExit, match="Windows"):
        runner.require_supported_platform()


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory):
    build = tmp_path_factory.mktemp("owner-runner-build")
    expected = {name: (ROOT / "scripts" / name).read_bytes() for name in HELPERS}
    command = subprocess.run(
        ["uv", "build", "--offline", "--sdist", "--out-dir", str(build)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert command.returncode == 0, command.stdout + command.stderr
    sdist = next(build.glob("*.tar.gz"))
    command = subprocess.run(
        ["uv", "build", "--offline", "--wheel", str(sdist), "--out-dir", str(build)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert command.returncode == 0, command.stdout + command.stderr
    return build, sdist, next(build.glob("*.whl")), expected


def test_wheel_from_sdist_contains_exact_canonical_helpers(built_artifacts):
    _, sdist, wheel, expected = built_artifacts
    with tarfile.open(sdist) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        for name, content in expected.items():
            assert archive.extractfile(f"{prefix}/scripts/{name}").read() == content
    with zipfile.ZipFile(wheel) as archive:
        for name, content in expected.items():
            assert archive.read(f"exomem/_service/{name}") == content


@pytest.mark.skipif(
    not held_fs.platform_support().supported or os.name == "nt" or not shutil.which("bash"),
    reason="supported native owner maintenance runner",
)
def test_installed_wheel_module_and_receipt_helper_help(built_artifacts, tmp_path):
    _, _, wheel, _ = built_artifacts
    venv = tmp_path / "installed"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, created.stderr
    python = venv / "bin" / "python"
    installed = subprocess.run(
        ["uv", "pip", "install", "--offline", "--python", str(python), "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stderr
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [str(python), "-m", "exomem.native_owner_maintenance_runner", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "accepted native-owner" in result.stdout
    located = subprocess.run(
        [
            str(python),
            "-c",
            'from exomem.native_owner_maintenance_runner import _script_path; print(_script_path().with_name("service-transition-receipt.py"))',
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert located.returncode == 0, located.stderr
    receipt_help = subprocess.run(
        [str(python), located.stdout.strip(), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert receipt_help.returncode == 0, receipt_help.stderr
    assert "create" in receipt_help.stdout

    missing_unit = subprocess.run(
        [
            str(python),
            "-m",
            "exomem.native_owner_maintenance_runner",
            "--unit-file",
            str(tmp_path / "missing.service"),
            "--request-id",
            "fixture-review",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing_unit.returncode != 0
    assert "--unit-file not found" in missing_unit.stderr
    receipt_args = [
        "--path",
        str(tmp_path / "transition.json"),
        "--service-id",
        "fixture-service",
        "--binding-path",
        str(tmp_path / "service.env"),
        "--state-root",
        str(tmp_path / "state"),
        "--vault",
        str(tmp_path / "vault"),
        "--target-port",
        "8765",
    ]
    created = subprocess.run(
        [
            str(python),
            located.stdout.strip(),
            "create",
            *receipt_args,
            "--port",
            "8765",
            "--worker-pid",
            str(os.getpid()),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr
    verified = subprocess.run(
        [str(python), located.stdout.strip(), "verify", *receipt_args, "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["phase"] == "captured"
