from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from benchmark_capabilities import (
    has_posix_executable_scripts,
    require_mount_type_inspection,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra" / "scripts" / "ansible_with_sops.sh"

# Every test here runs that shell script as a program. Windows has no shebang,
# so `subprocess` reports `OSError: [WinError 193] %1 is not a valid Win32
# application` before the runner's own refusals are ever reached. The runner is
# Linux/macOS operator tooling; there is nothing here for Windows to check.
pytestmark = pytest.mark.skipif(
    not has_posix_executable_scripts(),
    reason="the ansible runner is a shebang script, which is not a program here",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def test_ansible_secret_runner_requires_tmpfs_before_decrypting(tmp_path: Path) -> None:
    # The runner proves the workspace is tmpfs with `findmnt`, which macOS does
    # not ship. Without it the refusal under test is unreachable -- the runner
    # stops one step earlier, on its own missing-tool refusal.
    require_mount_type_inspection()

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
@pytest.mark.parametrize("shared_playbook", [False, True])
def test_ansible_secret_runner_decrypts_only_on_tmpfs_and_removes_files(
    tmp_path: Path, shared_playbook: bool,
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
# Ansible's controller-side temp files (copy content, module payloads with
# their arguments) carry the same secrets, so they must stay on tmpfs too.
local = pathlib.Path(os.environ['ANSIBLE_LOCAL_TEMP'])
assert stat.S_IMODE(local.stat().st_mode) == 0o700
assert os.statvfs(local).f_fsid == os.statvfs(os.environ['TEST_TMPFS_ROOT']).f_fsid
(local / 'ansible-local-1').mkdir()
(local / 'ansible-local-1' / 'content').write_text(os.environ['TEST_SENTINEL'])
pathlib.Path(os.environ['TEST_MARKER']).write_text(
    json.dumps({'args': sys.argv[1:], 'values': [json.loads(path.read_text()) for path in paths],
                'local_temp': str(local), 'config': os.environ.get('ANSIBLE_CONFIG'),
                'inject': os.environ.get('ANSIBLE_INJECT_FACT_VARS')})
)
""",
    )
    encrypted = tmp_path / "k3s-server-token.v1.sops.json"
    encrypted.write_text('{"sops":{}}', encoding="utf-8")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    playbook = tmp_path / "shared-control.yml" if shared_playbook else ROOT / "infra/ansible/site.yml"
    config = tmp_path / "shared-ansible.cfg" if shared_playbook else ROOT / "infra/ansible/ansible.cfg"
    if shared_playbook:
        playbook.write_text("[]\n", encoding="utf-8")
        config.write_text("[defaults]\ninject_facts_as_vars = False\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(RUNNER),
            "--inventory",
            str(inventory),
            "--vars",
            str(encrypted),
            *(["--playbook", str(playbook), "--config", str(config)] if shared_playbook else []),
            "--",
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "ANSIBLE_PLAYBOOK_BIN": str(fake_ansible),
            "ANSIBLE_INJECT_FACT_VARS": "True",
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
    assert not Path(invocation["local_temp"]).exists()
    # Run from any directory, the repository's configuration still applies; it
    # keeps module-returned facts from shadowing inventory variables.
    assert invocation["config"] == str(config)
    assert str(playbook) in invocation["args"]
    # An operator's environment cannot switch fact injection back on.
    assert invocation["inject"] is None
    assert stat.S_IMODE(RUNNER.stat().st_mode) & stat.S_IXUSR


def _load_active_ansible_vars():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "active_ansible_vars", ROOT / "infra" / "scripts" / "active_ansible_vars.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "selected,expected,refusal",
    [
        # Escrowing v2 activates nothing: the selection still says v1.
        ({"ansible.hosted-node.tang-keys.active": "v1"}, ["tang-keys.v1.sops.json"], None),
        ({"ansible.hosted-node.tang-keys.active": "v3"}, None, "the selected v3 file is missing"),
        ({"ansible.hosted-node.tang-key.active": "v1"}, None, "not an Ansible destination"),
    ],
)
def test_active_ansible_vars_follow_the_selection_not_the_newest_file(
    tmp_path: Path, selected: dict, expected: list | None, refusal: str | None
) -> None:
    module = _load_active_ansible_vars()
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"schema_version": 1, "secrets": {"tang": {"destinations": {
        "ansible.hosted-node.tang-keys.active": {
            "kind": "sops_ansible_vars",
            "target": "infra/secrets/ansible/tang-keys.{version}.sops.json",
        },
    }}}}), encoding="utf-8")
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"schema_version": 1, "destinations": selected}), encoding="utf-8")
    secrets = tmp_path / "infra" / "secrets" / "ansible"
    secrets.mkdir(parents=True)
    for version in ("v1", "v2"):
        (secrets / f"tang-keys.{version}.sops.json").write_text("{}", encoding="utf-8")

    if refusal is None:
        assert module.active_files("hosted-node", matrix, selection, tmp_path) == [
            secrets / name for name in expected
        ]
    else:
        with pytest.raises(ValueError, match=refusal):
            module.active_files("hosted-node", matrix, selection, tmp_path)
