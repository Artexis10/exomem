"""Operator tests use a private fake control socket and isolated release root."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="managed service operator is Linux-only")

ROOT = Path(__file__).resolve().parents[1]


def _wheel(
    path: Path,
    *,
    version: str = "9.9.9",
    name: str = "exomem",
    hash_algorithm: str = "sha256",
    signature: bool = False,
) -> Path:
    """Build the smallest installable local Exomem wheel for staging tests."""
    dist_info = f"exomem-{version}.dist-info"
    path.parent.mkdir(parents=True, exist_ok=True)
    members = {
        "exomem/__init__.py": b"",
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "\n".join(
        f"{name},{hash_algorithm}={urlsafe_b64encode(hashlib.new(hash_algorithm, content).digest()).decode().rstrip('=')},{len(content)}"
        for name, content in members.items()
    )
    members[f"{dist_info}/RECORD"] = (record + f"\n{dist_info}/RECORD,,\n").encode()
    if signature:
        members[f"{dist_info}/RECORD.jws"] = b"signature"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _operator(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    variables = os.environ.copy()
    variables["PYTHONPATH"] = str(ROOT / "src")
    variables["EXOMEM_STATE_ROOT"] = str(tmp_path / "state")
    if env:
        variables.update(env)
    return subprocess.run(
        [sys.executable, "-m", "exomem.service_upgrade", "--runtime-dir", str(tmp_path / "managed"), *args],
        cwd=ROOT,
        env=variables,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _control(
    tmp_path: Path,
    *,
    phase: str = "ready",
    count: int | None = None,
    launcher_python: str | None = None,
) -> tuple[list[dict], threading.Thread]:
    runtime = tmp_path / "managed"
    runtime.mkdir(mode=0o700)
    launcher = Path(launcher_python) if launcher_python else tmp_path / "launcher" / "bin" / "python"
    if launcher_python is None:
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)
    requests: list[dict] = []
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(runtime / "control.sock"))
            listener.listen(4)
            listener.settimeout(3)
            ready.set()
            while len(requests) < (count if count is not None else (2 if phase == "ready" else 1)):
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    return
                with client:
                    payload = client.makefile("rb").readline()
                    request = json.loads(payload)
                    requests.append(request)
                    if request["command"] == "upgrade":
                        response = {"ok": True, "phase": "ready", "active": request["target"]}
                    else:
                        response = {
                            "ok": True,
                            "phase": phase,
                            "active": {"python": str(launcher), "version": "0.1.0"},
                            "launcher_python": str(launcher),
                            "unit": "exomem.service",
                            "port": 8765,
                        }
                    client.sendall((json.dumps(response) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return requests, thread


def test_status_uses_private_control_socket(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path, phase="unavailable")
    result = _operator(tmp_path, "--status")
    thread.join(3)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["phase"] == "unavailable"
    assert requests == [{"command": "status"}]


def test_upgrade_stages_immutable_release_before_sending_target(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path)
    fake_uv = tmp_path / "uv"
    trace = tmp_path / "uv.trace"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_UV_TRACE\"\n"
        "if [ \"$1\" = venv ]; then\n"
        "  mkdir -p \"$4/bin\"\n"
        "  cat > \"$4/bin/python\" <<'STUB'\n"
        "#!/bin/sh\n"
        "echo '{\"version\": \"0.2.0\", \"state_descriptors\": [\"claims-store\"]}'\n"
        "STUB\n"
        "  chmod +x \"$4/bin/python\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    result = _operator(
        tmp_path,
        "--package-version", "0.2.0", "--profile", "lean",
        env={"FAKE_UV_TRACE": str(trace), "EXOMEM_UV": str(fake_uv)},
    )
    thread.join(3)
    assert result.returncode == 0, result.stderr
    assert [item["command"] for item in requests] == ["status", "upgrade"]
    target = requests[1]["target"]
    assert target["python"].startswith(str(tmp_path / "releases"))
    assert target["version"] == "0.2.0"
    # The staged target carries its state-migration declaration, so the
    # supervisor can skip the offline migrator when nothing changed.
    assert target["state_descriptors"] == ["claims-store"]
    assert "pip install" in trace.read_text(encoding="utf-8")
    assert str(tmp_path / "launcher") not in trace.read_text(encoding="utf-8").splitlines()[-1]


def test_unavailable_manager_fails_before_creating_release(tmp_path: Path) -> None:
    result = _operator(tmp_path, "--package-version", "0.2.0")
    assert result.returncode != 0
    assert not (tmp_path / "releases").exists()


def test_staging_failure_never_requests_handoff(tmp_path: Path) -> None:
    requests, thread = _control(tmp_path, count=1)
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_uv.chmod(0o700)
    result = _operator(tmp_path, "--package-version", "0.2.0", env={"EXOMEM_UV": str(fake_uv)})
    thread.join(3)
    assert result.returncode != 0
    assert requests == [{"command": "status"}]


def test_operator_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    runtime = tmp_path / "managed"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    outside.chmod(0o644)
    (runtime / "operator.lock").symlink_to(outside)
    result = _operator(tmp_path, "--package-version", "0.2.0")
    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert outside.stat().st_mode & 0o777 == 0o644


def test_stage_failure_surfaces_diagnostic_tail_but_scrubs_credentials(tmp_path: Path) -> None:
    """uv's stderr now helps diagnose staging failures (e.g. a stale index),

    but anything credential-shaped in that output must still never reach the
    operator's own stderr.
    """
    requests, thread = _control(tmp_path, count=1)
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "echo 'stale package index: exomem 0.2.0 not found (HTTP 404)' >&2\n"
        "echo 'retry url: https://deploy:S3cr3tTok3n9876@pypi.example.com/simple/exomem/' >&2\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    result = _operator(tmp_path, env={"EXOMEM_UV": str(fake_uv)})
    thread.join(3)
    assert result.returncode != 0
    assert requests == [{"command": "status"}]
    assert "stale package index" in result.stderr
    assert "S3cr3tTok3n9876" not in result.stderr
    assert "deploy:" not in result.stderr


def test_stage_failure_truncates_large_uv_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import service_upgrade

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        f"#!/bin/sh\n{sys.executable} -c \"import sys; sys.stderr.write(('x' * 1023 + '\\\\n') * 1024)\"\nexit 2\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("EXOMEM_UV", str(fake_uv))
    with pytest.raises(RuntimeError) as excinfo:
        service_upgrade.stage(tmp_path / "managed", sys.executable, "lean", "0.2.0")
    message = str(excinfo.value)
    tail = message.split("uv stderr: ", 1)[1]
    assert len(tail.encode("utf-8")) <= 4096


def test_stage_failure_scrubs_url_userinfo_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import service_upgrade

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "echo 'retry url: https://deploy:S3cr3tTok3n9876@pypi.example.com/simple/exomem/' >&2\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("EXOMEM_UV", str(fake_uv))
    with pytest.raises(RuntimeError) as excinfo:
        service_upgrade.stage(tmp_path / "managed", sys.executable, "lean", "0.2.0")
    message = str(excinfo.value)
    assert "S3cr3tTok3n9876" not in message
    assert "deploy:" not in message
    assert "pypi.example.com" in message


def test_stage_failure_surfaces_uv_stderr_tail_from_fake_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import service_upgrade

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\necho 'stale package index: exomem 0.2.0 not found (HTTP 404)' >&2\nexit 2\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("EXOMEM_UV", str(fake_uv))
    with pytest.raises(RuntimeError, match="stale package index"):
        service_upgrade.stage(tmp_path / "managed", sys.executable, "lean", "0.2.0")


def test_stage_success_is_unchanged_when_uv_writes_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import service_upgrade

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = venv ]; then\n"
        "  mkdir -p \"$4/bin\"\n"
        "  cat > \"$4/bin/python\" <<'STUB'\n"
        "#!/bin/sh\n"
        "echo '{\"version\": \"0.2.0\", \"state_descriptors\": []}'\n"
        "STUB\n"
        "  chmod +x \"$4/bin/python\"\n"
        "fi\n"
        "echo 'noise on stderr that must not affect success' >&2\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("EXOMEM_UV", str(fake_uv))
    target = service_upgrade.stage(tmp_path / "managed", sys.executable, "lean", "0.2.0")
    assert target["version"] == "0.2.0"


def test_uv_stderr_tail_never_leaks_a_password_split_by_the_byte_clip() -> None:
    """A clip landing inside `https://user:` must not strip the scheme the
    userinfo scrub depends on and then emit the password."""
    from exomem import service_upgrade

    url = b"https://deploy:S3cr3tTok3n9876@pypi.example.com/simple/ "
    for offset in range(0, len(url)):
        data = url + b"y" * (4096 - len(url) + offset)
        tail = service_upgrade._uv_stderr_tail(data)
        assert "S3cr3tTok3n9876" not in tail, offset
        assert "ploy:" not in tail, offset


def test_uv_stderr_tail_drops_a_leading_partial_line_from_the_read_window() -> None:
    from exomem import service_upgrade

    # The credential line straddles the start of the bounded read window, so
    # only its `ploy:S3cr3t…@host` remainder is inside it.
    url = b"https://deploy:S3cr3tTok3n9876@pypi.example.com/simple/"
    last = b"\nerror: stale package index\n"
    window = service_upgrade._UV_STDERR_READ_WINDOW_BYTES
    padding = b"y" * (window - (len(url) - 10) - 1 - len(last))
    data = b"z" * (200 * 1024) + url + b" " + padding + last
    assert data[-window:].startswith(b"ploy:S3cr3t")
    tail = service_upgrade._uv_stderr_tail(data)
    assert "S3cr3tTok3n9876" not in tail
    assert tail == "error: stale package index"


def test_runtime_symlink_is_refused_before_operator_lock_creation(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    (tmp_path / "managed").symlink_to(actual, target_is_directory=True)
    result = _operator(tmp_path)
    assert result.returncode != 0
    assert not (actual / "operator.lock").exists()


def test_help_does_not_require_a_running_manager(tmp_path: Path) -> None:
    result = _operator(tmp_path, "--help")
    assert result.returncode == 0
    assert "--status" in result.stdout and "--resume" in result.stdout


def test_a_release_without_the_descriptor_probe_still_stages(tmp_path: Path) -> None:
    """Rollback to a pre-change release must not fail at staging.

    The probe reads the target's state-migration declaration. A release that
    predates it has no `declared_descriptor_ids`, and an unguarded import made
    `_staged_identity` exit non-zero, so every downgrade failed before it began.
    An empty declaration is the right answer: the supervisor then runs the
    offline migrator.
    """
    import os
    import subprocess
    import sys

    from exomem import service_upgrade

    legacy = tmp_path / "legacy"
    (legacy / "exomem").mkdir(parents=True)
    (legacy / "exomem" / "__init__.py").write_text("", encoding="utf-8")
    # The pre-change module: no `declared_descriptor_ids` to import.
    (legacy / "exomem" / "state_migration.py").write_text("", encoding="utf-8")

    interpreter = tmp_path / "legacy-python"
    interpreter.write_text(
        f'#!/bin/sh\nPYTHONPATH="{legacy}" exec "{sys.executable}" -c "$3"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o700)

    probe = subprocess.run(
        [str(interpreter), "-I", "-c", service_upgrade._TARGET_PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "PYTHONPATH": str(legacy)},
    )
    assert probe.returncode == 0, probe.stderr[-2000:]

    identity = service_upgrade._staged_identity(interpreter)
    assert identity["state_descriptors"] == []
    assert identity["version"]


def test_wheel_requires_a_full_source_revision(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    result = _operator(tmp_path, "--wheel", str(wheel), "--source-revision", "deadbeef")

    assert result.returncode == 2
    assert "full hexadecimal source revision" in result.stderr


def test_wheel_and_published_version_are_mutually_exclusive(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    result = _operator(
        tmp_path,
        "--wheel",
        str(wheel),
        "--source-revision",
        "a" * 40,
        "--package-version",
        "9.9.9",
    )

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


def test_wheel_package_mismatch_refuses_staging(tmp_path: Path) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl", name="not-exomem")
    with pytest.raises(RuntimeError, match="not an Exomem wheel"):
        service_upgrade.stage(tmp_path / "managed", sys.executable, "lean", "", wheel, "a" * 40)


def test_wheel_source_mutation_refuses_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    original_sha256 = service_upgrade._sha256_file
    calls = 0

    def mutate_after_first_hash(path: Path) -> str:
        nonlocal calls
        digest = original_sha256(path)
        if path == wheel:
            calls += 1
            if calls == 1:
                with path.open("ab") as stream:
                    stream.write(b"changed")
        return digest

    monkeypatch.setattr(service_upgrade, "_sha256_file", mutate_after_first_hash)
    with pytest.raises(RuntimeError, match="changed while staging"):
        service_upgrade._snapshot_wheel(wheel, release)


def test_wheel_staging_snapshots_provenance_before_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "wheel source" / "exomem-9.9.9-py3-none-any.whl")
    original_mode = wheel.stat().st_mode
    revision = "a" * 40
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))

    target = service_upgrade.stage(
        tmp_path / "managed",
        sys.executable,
        "lean",
        "",
        wheel,
        revision,
    )

    staged_release = Path(target["python"]).parents[2]
    provenance = json.loads((staged_release / "provenance.json").read_text(encoding="utf-8"))
    assert provenance == {
        "artifact_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "source_revision": revision,
        "original_artifact_name": wheel.name,
        "installed_name": "exomem",
        "installed_version": "9.9.9",
        "installed_python": target["python"],
    }
    assert stat.S_IMODE((staged_release / "provenance.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(wheel.stat().st_mode) == stat.S_IMODE(original_mode)


def test_wheel_snapshot_mutation_after_install_refuses_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import service_upgrade

    artifact = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    preinstall_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = venv ]; then\n"
        "  mkdir -p \"$4/bin\"\n"
        "  touch \"$4/bin/python\"\n"
        "  chmod +x \"$4/bin/python\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("EXOMEM_UV", str(fake_uv))

    def mutate_snapshot(target_python: Path, *, wheel: bool) -> dict[str, object]:
        assert wheel
        snapshot = target_python.parents[2] / artifact.name
        with snapshot.open("ab") as stream:
            stream.write(b"changed after install")
        return {
            "version": "9.9.9",
            "name": "exomem",
            "direct_url": {
                "url": f"{snapshot.as_uri()}#sha256={preinstall_digest}",
                "archive_info": {},
            },
            "record_hashes": service_upgrade._wheel_record_hashes(snapshot),
            "state_descriptors": [],
        }

    monkeypatch.setattr(service_upgrade, "_staged_identity", mutate_snapshot)
    with pytest.raises(RuntimeError, match="snapshot changed while staging"):
        service_upgrade.stage(tmp_path / "managed", sys.executable, "lean", "", artifact, "a" * 40)


def test_wheel_install_accepts_current_pep_610_hashes(tmp_path: Path) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    digest = service_upgrade._sha256_file(wheel)
    identity = {
        "version": "9.9.9",
        "name": "exomem",
        "direct_url": {
            "url": wheel.as_uri(),
            "archive_info": {"hashes": {"sha256": digest}},
        },
        "record_hashes": service_upgrade._wheel_record_hashes(wheel),
    }

    service_upgrade._verify_wheel_install(identity, wheel, digest, "9.9.9")
    identity["direct_url"]["archive_info"]["hashes"]["sha256"] = "mismatch"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="digest does not match"):
        service_upgrade._verify_wheel_install(identity, wheel, digest, "9.9.9")


def test_wheel_install_refuses_mismatched_installed_file_hashes(tmp_path: Path) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    digest = service_upgrade._sha256_file(wheel)
    record_hashes = service_upgrade._wheel_record_hashes(wheel)
    record_hashes["exomem/__init__.py"] = "mismatch"
    identity = {
        "version": "9.9.9",
        "name": "exomem",
        "direct_url": {
            "url": wheel.as_uri(),
            "archive_info": {"hash": f"sha256={digest}"},
        },
        "record_hashes": record_hashes,
    }

    with pytest.raises(RuntimeError, match="files do not match"):
        service_upgrade._verify_wheel_install(identity, wheel, digest, "9.9.9")


def test_published_identity_probe_does_not_hash_installed_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import service_upgrade

    commands: list[list[str]] = []

    def probe(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"version": "9.9.9", "state_descriptors": []}',
        )

    monkeypatch.setattr(service_upgrade.subprocess, "run", probe)
    assert service_upgrade._staged_identity(Path("/tmp/python"))["version"] == "9.9.9"
    assert commands[0][-1] == service_upgrade._TARGET_PROBE
    assert "RECORD" not in service_upgrade._TARGET_PROBE


def test_wheel_source_replacement_after_metadata_refuses_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl")
    replacement = _wheel(tmp_path / "replacement.whl")
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    original_metadata = service_upgrade._wheel_metadata

    def replace_after_metadata(path: Path) -> tuple[str, str]:
        metadata = original_metadata(path)
        if path == wheel:
            os.replace(replacement, wheel)
        return metadata

    monkeypatch.setattr(service_upgrade, "_wheel_metadata", replace_after_metadata)
    with pytest.raises(RuntimeError, match="changed while staging"):
        service_upgrade._snapshot_wheel(wheel, release)


@pytest.mark.parametrize("hash_algorithm", ["sha256", "sha384", "sha512"])
def test_wheel_staging_accepts_secure_record_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hash_algorithm: str
) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl", hash_algorithm=hash_algorithm)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))

    target = service_upgrade.stage(
        tmp_path / "managed", sys.executable, "lean", "", wheel, "a" * 40
    )

    assert target["version"] == "9.9.9"


def test_wheel_record_signatures_do_not_break_content_proof(tmp_path: Path) -> None:
    from exomem import service_upgrade

    wheel = _wheel(tmp_path / "exomem-9.9.9-py3-none-any.whl", signature=True)

    assert "exomem/__init__.py" in service_upgrade._wheel_record_hashes(wheel)


def test_local_wheel_operator_handoff_keeps_manager_target_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel(tmp_path / "wheel source" / "exomem-9.9.9-py3-none-any.whl")
    requests, thread = _control(tmp_path, launcher_python=sys.executable)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    result = _operator(
        tmp_path,
        "--wheel",
        str(wheel),
        "--source-revision",
        "a" * 40,
        "--profile",
        "lean",
        env={"UV_CACHE_DIR": str(tmp_path / "uv-cache")},
    )
    thread.join(3)

    assert result.returncode == 0, result.stderr
    assert [request["command"] for request in requests] == ["status", "upgrade"]
    target = requests[1]["target"]
    assert set(target) == {"python", "version", "state_descriptors"}
    assert target["version"] == "9.9.9"
