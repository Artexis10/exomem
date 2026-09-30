"""A second service on the same machine must be upgradable without touching the first.

A client cell installed beside the default service (its own unit, port and
vault) was upgraded with `scripts/upgrade.sh --unit-file`. Three defaults
assumed the default service: the vault came from the checkout's `.env`, the
managed-install manifest the CLI and TUI route by was rewritten to the second
service, and `--cli-sync auto` realigned the one uv-tool CLI. The CLI sync also
replaced an operator-owned launcher in uv's bin directory with uv's symlink.

Every fake here lives under tmp_path; nothing touches a real unit, service or CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_service-common.sh"
UPGRADE = ROOT / "scripts" / "upgrade.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or os.name == "nt", reason="systemd unit handling is exercised on Unix"
)


def _bash(body: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, "-c", f'set -euo pipefail; . "{COMMON}"; {body}'],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _home(tmp_path: Path) -> dict[str, str]:
    """An environment whose default unit and manifest live under tmp_path."""
    config = tmp_path / "config"
    (config / "systemd" / "user").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXOMEM_")}
    env["XDG_CONFIG_HOME"] = str(config)
    env["EXOMEM_MANAGED_INSTALL_MANIFEST"] = str(config / "exomem" / "managed-install.json")
    return env


def _unit(tmp_path: Path, name: str, *, env_file: str | None, environment: str = "") -> Path:
    unit = tmp_path / "config" / "systemd" / "user" / f"{name}.service"
    lines = ["[Service]"]
    if environment:
        lines.append(environment)
    if env_file is not None:
        lines.append(f"EnvironmentFile={env_file}")
    lines.append('ExecStart="/nonexistent/python" -m exomem --port 8766')
    unit.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return unit


def test_unit_env_value_reads_the_units_own_environment_file(tmp_path: Path) -> None:
    env = _home(tmp_path)
    env_file = tmp_path / "second.env"
    env_file.write_text('EXOMEM_VAULT_PATH="/vaults/second"\nOTHER=1\n', encoding="utf-8")
    unit = _unit(
        tmp_path,
        "exomem-second",
        env_file=str(env_file),
        environment='Environment="EXOMEM_VAULT_PATH=/vaults/overridden"',
    )

    result = _bash(f'exomem_unit_env_value "{unit}" EXOMEM_VAULT_PATH', env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/vaults/second"


def test_unit_env_value_falls_back_to_environment_lines_and_optional_files(tmp_path: Path) -> None:
    env = _home(tmp_path)
    unit = _unit(
        tmp_path,
        "exomem-second",
        env_file=f"-{tmp_path / 'missing.env'}",
        environment="Environment=EXOMEM_VAULT_PATH=/vaults/inline",
    )

    result = _bash(f'exomem_unit_env_value "{unit}" EXOMEM_VAULT_PATH', env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/vaults/inline"


def test_unit_env_value_splits_multiple_assignments_like_systemd(tmp_path: Path) -> None:
    env = _home(tmp_path)
    bare = _unit(
        tmp_path,
        "exomem-bare",
        env_file=None,
        environment="Environment=EXOMEM_VAULT_PATH=/vaults/bare OTHER=1",
    )
    quoted = _unit(
        tmp_path,
        "exomem-quoted",
        env_file=None,
        environment='Environment="EXOMEM_PORT=8766" "EXOMEM_VAULT_PATH=/vaults/with space"',
    )

    assert _bash(f'exomem_unit_env_value "{bare}" EXOMEM_VAULT_PATH', env).stdout == "/vaults/bare"
    assert _bash(f'exomem_unit_env_value "{quoted}" EXOMEM_VAULT_PATH', env).stdout == (
        "/vaults/with space"
    )


def test_only_the_default_unit_counts_as_default(tmp_path: Path) -> None:
    env = _home(tmp_path)
    default = _unit(tmp_path, "exomem", env_file=None)
    second = _unit(tmp_path, "exomem-second", env_file=None)

    assert _bash(f'exomem_unit_is_default "{default}"', env).returncode == 0
    assert _bash(f'exomem_unit_is_default "{second}"', env).returncode != 0


def _manifest(env: dict[str, str]) -> Path:
    return Path(env["EXOMEM_MANAGED_INSTALL_MANIFEST"])


def _write_owned(env: dict[str, str], unit: Path, target: str) -> subprocess.CompletedProcess[str]:
    return _bash(
        f'exomem_write_owned_managed_manifest "{unit}" python3 1.2.3 media "{target}"', env
    )


def test_second_service_leaves_the_default_services_manifest_alone(tmp_path: Path) -> None:
    env = _home(tmp_path)
    second = _unit(tmp_path, "exomem-second", env_file=None)
    manifest = _manifest(env)
    manifest.parent.mkdir(parents=True)
    original = {"schema_version": 1, "service_target": "http://127.0.0.1:8765"}
    manifest.write_text(json.dumps(original), encoding="utf-8")

    result = _write_owned(env, second, "http://127.0.0.1:8766")

    assert result.returncode == 0, result.stderr
    assert "left on http://127.0.0.1:8765" in result.stdout
    assert json.loads(manifest.read_text(encoding="utf-8")) == original


def test_second_service_does_not_claim_an_unreadable_manifest(tmp_path: Path) -> None:
    env = _home(tmp_path)
    second = _unit(tmp_path, "exomem-second", env_file=None)
    manifest = _manifest(env)
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{not json", encoding="utf-8")

    result = _write_owned(env, second, "http://127.0.0.1:8766")

    assert result.returncode == 0, result.stderr
    assert manifest.read_text(encoding="utf-8") == "{not json"


def test_second_service_updates_a_manifest_that_already_names_it(tmp_path: Path) -> None:
    env = _home(tmp_path)
    second = _unit(tmp_path, "exomem-second", env_file=None)
    manifest = _manifest(env)
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"service_target": "http://127.0.0.1:8766"}), encoding="utf-8")

    result = _write_owned(env, second, "http://127.0.0.1:8766")

    assert result.returncode == 0, result.stderr
    assert json.loads(manifest.read_text(encoding="utf-8"))["service_version"] == "1.2.3"


def test_default_unit_always_owns_the_manifest(tmp_path: Path) -> None:
    env = _home(tmp_path)
    default = _unit(tmp_path, "exomem", env_file=None)
    manifest = _manifest(env)
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"service_target": "http://127.0.0.1:8766"}), encoding="utf-8")

    result = _write_owned(env, default, "http://127.0.0.1:8765")

    assert result.returncode == 0, result.stderr
    assert json.loads(manifest.read_text(encoding="utf-8"))["service_target"] == (
        "http://127.0.0.1:8765"
    )


def test_cli_sync_keeps_an_operator_owned_launcher(tmp_path: Path) -> None:
    env = _home(tmp_path)
    bin_dir = tmp_path / "bin"
    tool_env = tmp_path / "tool-env"
    bin_dir.mkdir()
    tool_env.mkdir()
    launcher = bin_dir / "exomem"
    launcher.write_text("#!/bin/sh\n# operator launcher\n", encoding="utf-8")
    launcher.chmod(0o755)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    uv = fakes / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        "  'tool list') echo 'exomem v0.1.0' ;;\n"
        f"  'tool dir') echo '{bin_dir}' ;;\n"
        "  'tool install')\n"
        f"    for n in exomem kb; do touch '{tool_env}'/$n; ln -sf '{tool_env}'/$n '{bin_dir}'/$n; done ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env["PATH"] = f"{fakes}{os.pathsep}{env['PATH']}"

    result = _bash("exomem_sync_uv_cli auto 1.2.3", env)

    assert result.returncode == 0, result.stderr
    assert not launcher.is_symlink()
    assert "operator launcher" in launcher.read_text(encoding="utf-8")
    assert os.access(launcher, os.X_OK)
    assert (bin_dir / "kb").is_symlink(), "a uv-owned entry point is still uv's to replace"


def _run_upgrade(
    tmp_path: Path, env: dict[str, str], unit: Path
) -> subprocess.CompletedProcess[str]:
    python = tmp_path / "venv-python"
    # Reports an installed version, and runs the unit parser like a real venv would.
    python.write_text(
        '#!/bin/sh\nif [ "$1" = - ]; then exec python3 "$@"; fi\necho 0.1.0\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    unit.write_text(
        unit.read_text(encoding="utf-8").replace("/nonexistent/python", str(python)),
        encoding="utf-8",
    )
    env = dict(env, EXOMEM_VAULT_PATH="/vaults/from-shell")
    assert BASH is not None
    return subprocess.run(
        [BASH, str(UPGRADE), "--unit-file", str(unit), "--cli-sync", "auto"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )


def test_upgrade_refuses_to_guess_a_second_services_vault(tmp_path: Path) -> None:
    env = _home(tmp_path)
    second = _unit(tmp_path, "exomem-second", env_file=None)

    result = _run_upgrade(tmp_path, env, second)

    assert result.returncode != 0
    assert "pass --vault for a service that is not the default unit" in result.stderr
    assert "leaving the uv-tool CLI alone" in result.stdout


def test_upgrade_source_resolves_the_vault_from_the_unit_first() -> None:
    upgrade = UPGRADE.read_text(encoding="utf-8")

    unit_lookup = upgrade.index('exomem_unit_env_value "$UNIT_FILE" EXOMEM_VAULT_PATH')
    assert unit_lookup < upgrade.index('exomem_dotenv_value "$REPO_ROOT" EXOMEM_VAULT_PATH')
    assert "exomem_write_managed_manifest" not in upgrade
    assert upgrade.count("exomem_write_owned_managed_manifest") == 2
