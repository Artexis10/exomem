from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra" / "scripts" / "ansible_with_sops.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def test_ansible_secret_runner_requires_tmpfs_before_decrypting(tmp_path: Path) -> None:
    encrypted = tmp_path / "secret.v1.sops.json"
    encrypted.write_text('{"sops":{}}', encoding="utf-8")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(RUNNER),
            "--inventory",
            str(inventory),
            "--vars",
            str(encrypted),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "EXOMEM_SECRET_TMPFS_DIR": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "must be tmpfs or ramfs" in result.stderr


@pytest.mark.parametrize(
    "passthrough",
    [
        ("--extra", "k3s_server_token=must-not-reach-argv"),
        ("--extra-v", "k3s_server_token=must-not-reach-argv"),
        ("--extra-var", "k3s_server_token=must-not-reach-argv"),
        ("--extra-vars", "k3s_server_token=must-not-reach-argv"),
        ("--extra=k3s_server_token=must-not-reach-argv",),
        ("--extra-v=k3s_server_token=must-not-reach-argv",),
        ("--extra-var=k3s_server_token=must-not-reach-argv",),
        ("--extra-vars=k3s_server_token=must-not-reach-argv",),
        ("-e", "k3s_server_token=must-not-reach-argv"),
        ("-ek3s_server_token=must-not-reach-argv",),
    ],
    ids=[
        "extra",
        "extra-v",
        "extra-var",
        "extra-vars",
        "extra-equals",
        "extra-v-equals",
        "extra-var-equals",
        "extra-vars-equals",
        "short-separate",
        "short-attached",
    ],
)
def test_ansible_secret_runner_rejects_extra_vars_passthrough(
    tmp_path: Path,
    passthrough: tuple[str, ...],
) -> None:
    encrypted = tmp_path / "secret.v1.sops.json"
    encrypted.write_text('{"sops":{}}', encoding="utf-8")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(RUNNER),
            "--inventory",
            str(inventory),
            "--vars",
            str(encrypted),
            "--",
            *passthrough,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "EXOMEM_SECRET_TMPFS_DIR": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "Ansible passthrough must not include extra vars" in result.stderr
    assert "must-not-reach-argv" not in result.stdout + result.stderr


@pytest.mark.skipif(not Path("/dev/shm").is_dir(), reason="tmpfs is unavailable")
def test_ansible_secret_runner_decrypts_only_on_tmpfs_and_removes_files(
    tmp_path: Path,
) -> None:
    fake_sops = tmp_path / "sops"
    fake_ansible = tmp_path / "ansible-playbook"
    marker = tmp_path / "ansible.json"
    decrypted_paths = tmp_path / "decrypted-paths.txt"
    sentinel = "tmpfs-only-secret"
    _write_executable(
        fake_sops,
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index('--output') + 1])
output.write_text(json.dumps({'k3s_server_token': os.environ['TEST_SENTINEL']}))
with pathlib.Path(os.environ['TEST_DECRYPTED_PATHS']).open('a') as handle:
    handle.write(str(output) + '\\n')
""",
    )
    _write_executable(
        fake_ansible,
        """#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import sys

paths = [pathlib.Path(arg[1:]) for arg in sys.argv[1:] if arg.startswith('@')]
for path in paths:
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.statvfs(path).f_fsid == os.statvfs(os.environ['TEST_TMPFS_ROOT']).f_fsid
pathlib.Path(os.environ['TEST_MARKER']).write_text(
    json.dumps({'args': sys.argv[1:], 'values': [json.loads(path.read_text()) for path in paths]})
)
""",
    )
    encrypted = tmp_path / "k3s-server-token.v1.sops.json"
    encrypted.write_text('{"sops":{}}', encoding="utf-8")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(RUNNER),
            "--inventory",
            str(inventory),
            "--vars",
            str(encrypted),
            "--",
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "ANSIBLE_PLAYBOOK_BIN": str(fake_ansible),
            "EXOMEM_SECRET_TMPFS_DIR": "/dev/shm",
            "SOPS_BIN": str(fake_sops),
            "TEST_DECRYPTED_PATHS": str(decrypted_paths),
            "TEST_MARKER": str(marker),
            "TEST_SENTINEL": sentinel,
            "TEST_TMPFS_ROOT": "/dev/shm",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    invocation = json.loads(marker.read_text(encoding="utf-8"))
    assert invocation["values"] == [{"k3s_server_token": sentinel}]
    assert "--check" in invocation["args"]
    for path in decrypted_paths.read_text(encoding="utf-8").splitlines():
        assert not Path(path).exists()
    assert stat.S_IMODE(RUNNER.stat().st_mode) & stat.S_IXUSR
