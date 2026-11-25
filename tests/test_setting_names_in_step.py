"""Every reader of a setting's two names chooses like `env_compat`.

The package reads `EXOMEM_X`, else `EVERMATTER_X`, else `KB_MCP_X`, and refuses a conflict in a
setting that picks data. The hooks, the service scripts (bash and PowerShell), cellctl and the
chart's scheduler cannot import the package, so each carries its own copy of that rule. This
module settles one sample and one matrix through every copy and compares the answers with the
package's. A pull request runs it as a guard module (`tests/pr_guard_modules.txt`), so a change to
one copy cannot merge without the others.
"""

from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from exomem import env_compat

ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = ROOT / "scripts" / "_service-common.sh"
COMMON_PS1 = ROOT / "scripts" / "_service-common.ps1"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")

needs_bash = pytest.mark.skipif(BASH is None or os.name == "nt", reason="the service scripts run under bash")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not available")
needs_systemd_layout = pytest.mark.skipif(
    BASH is None or platform.system() != "Linux", reason="a systemd unit is bound on Linux only"
)

# One sample that every copy of the rename rule must settle like `env_compat`: aliases alone,
# precedence between aliases, agreeing and differing pairs, a silent legacy loss and empty values.
ALIAS_SAMPLE = {
    "EVERMATTER_PRODUCT_ONLY": "product",
    "EVERMATTER_BEATS_LEGACY": "product-wins",
    "KB_MCP_BEATS_LEGACY": "legacy-loses",
    "KB_MCP_LEGACY_ONLY": "legacy",
    "EVERMATTER_SAME": "same",
    "EXOMEM_SAME": "same",
    "EVERMATTER_DIFFERS": "alias",
    "EXOMEM_DIFFERS": "explicit",
    "KB_MCP_QUIET": "legacy",
    "EXOMEM_QUIET": "explicit",
    "EVERMATTER_EMPTY": "",
    "EVERMATTER_EMPTY_DIFFERS": "",
    "EXOMEM_EMPTY_DIFFERS": "explicit",
    "EXOMEM_UNRELATED": "kept",
    "OTHER_SETTING": "ignored",
}


def _load_by_path(name: str, relative: str):
    """A module that ships apart from the package (cellctl's, the chart's), loaded without its siblings."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_standalone_hook_renames_like_env_compat() -> None:
    """A hook cannot import env_compat, so it carries a copy of the rule; one sample keeps them alike."""
    from exomem._hooks import (
        exomem_capture_nudge,
        exomem_continuation_checkpoint,
        exomem_local_credentials,
        exomem_retrieve_nudge,
    )

    renamed, conflicts = env_compat.rename_settings(ALIAS_SAMPLE)
    assert renamed["EXOMEM_PRODUCT_ONLY"] == "product"
    assert [c.alias for c in conflicts] == ["EVERMATTER_DIFFERS", "EVERMATTER_EMPTY_DIFFERS"]

    for hook in (
        exomem_capture_nudge,
        exomem_retrieve_nudge,
        exomem_continuation_checkpoint,
        exomem_local_credentials,
    ):
        environ = dict(ALIAS_SAMPLE)
        names = hook._rename_aliases(environ)
        assert environ == renamed, hook.__name__
        assert sorted(names) == sorted(c.alias for c in conflicts), hook.__name__


def test_the_hooks_skip_on_the_package_placement_settings_and_the_hook_home() -> None:
    """The hooks skip a turn only for a setting that picks data, so their closed set must match the package's."""
    from exomem._hooks import (
        exomem_capture_nudge,
        exomem_continuation_checkpoint,
        exomem_retrieve_nudge,
    )

    for hook in (exomem_capture_nudge, exomem_retrieve_nudge, exomem_continuation_checkpoint):
        assert hook._PLACEMENT_SETTINGS == env_compat.PLACEMENT_SETTINGS | {"HOOK_HOME"}, hook.__name__


def test_the_cellctl_and_scheduler_readers_choose_like_env_compat(capsys, caplog) -> None:
    """cellctl and the chart's scheduler cannot import env_compat, so each carries a copy of the rule."""
    cellctl = _load_by_path("cellctl_setting_alias", "infra/cellctl/src/cellctl/setting_alias.py")
    scheduler = _load_by_path(
        "scheduler_runtime_alias_probe", "infra/helm/platform/files/scheduler_runtime.py"
    )
    renamed, conflicts = env_compat.rename_settings(ALIAS_SAMPLE)
    internal_names = sorted(name for name in renamed if name.startswith("EXOMEM_"))
    warned = {c.alias for c in conflicts}

    def aliases_in(text: str) -> set[str]:
        return set(re.findall(r"EVERMATTER_\w+", text))

    for name in ALIAS_SAMPLE:
        assert cellctl.canonical_name(name) == next(iter(env_compat.rename_settings({name: "v"})[0])), name

    with mock.patch.dict(os.environ, ALIAS_SAMPLE, clear=True), caplog.at_level(logging.WARNING):
        for name in internal_names:
            assert cellctl.setting(name) == renamed[name], name
        assert aliases_in(caplog.text) == warned
        capsys.readouterr()
        for name in internal_names:
            assert scheduler._setting(name) == renamed[name], name
        assert aliases_in(capsys.readouterr().err) == warned


# The matrix: every setting that picks data (the package's placement set, so a setting added there
# is pinned in each copy) and one that does not (PORT), each with every combination of the three
# names absent, empty, "a" or "b".
SUFFIXES = (*sorted(env_compat.PLACEMENT_SETTINGS), "PORT")
PREFIXES = ("EXOMEM_", "EVERMATTER_", "KB_MCP_")
VALUES = (None, "", "a", "b")
CASES = [
    (suffix, dict(zip(PREFIXES, combination, strict=True)))
    for suffix in SUFFIXES
    for combination in itertools.product(VALUES, repeat=len(PREFIXES))
]


def _expected(suffix: str, names: dict[str, str | None]) -> str:
    """What the package reads for this case: its value, or `refused` for a placement conflict."""
    settings = {f"{prefix}{suffix}": value for prefix, value in names.items() if value is not None}
    renamed, conflicts = env_compat.rename_settings(settings)
    if any(env_compat.is_placement(conflict) for conflict in conflicts):
        return "refused"
    return f"value:{renamed.get(f'EXOMEM_{suffix}', '')}"


def _dotenv(path: Path, suffix: str, names: dict[str, str | None]) -> None:
    path.write_text(
        "".join(f"{prefix}{suffix}={value}\n" for prefix, value in names.items() if value is not None),
        encoding="utf-8",
    )


@needs_bash
def test_the_shell_readers_choose_like_env_compat(tmp_path) -> None:
    """One bash run settles every case through the dotenv reader and the process reader."""
    lines = [f'. "{COMMON_SH}"']
    for index, (suffix, names) in enumerate(CASES):
        _dotenv(tmp_path / f"{index}.env", suffix, names)
        process = " ".join(
            f"{prefix}{suffix}={value}" for prefix, value in names.items() if value is not None
        )
        lines.append(
            f"""
out="$(exomem_dotenv_file_value "{tmp_path}/{index}.env" EXOMEM_{suffix} 2>/dev/null)"; rc=$?
echo "dotenv|{index}|$rc|$out"
out="$(env -i {process} "{BASH}" -c '. "{COMMON_SH}"; exomem_process_setting EXOMEM_{suffix}' 2>/dev/null)"; rc=$?
echo "process|{index}|$rc|$out"
"""
        )
    out = subprocess.run(
        [BASH, "-c", "\n".join(lines)], capture_output=True, text=True, timeout=300, check=False
    )

    answers = {}
    for line in out.stdout.splitlines():
        reader, index, rc, value = line.split("|", 3)
        answers[(reader, int(index))] = (int(rc), value)
    assert len(answers) == 2 * len(CASES), out.stderr
    for index, (suffix, names) in enumerate(CASES):
        for reader in ("dotenv", "process"):
            rc, value = answers[(reader, index)]
            got = "refused" if rc == 2 else f"value:{value}"
            assert got == _expected(suffix, names), (reader, suffix, names, rc, value)


@needs_pwsh
def test_the_powershell_readers_choose_like_env_compat(tmp_path) -> None:
    """One pwsh run settles every case through the dotenv reader."""
    rows = []
    for index, (suffix, names) in enumerate(CASES):
        directory = tmp_path / f"d{index}"
        directory.mkdir()
        _dotenv(directory / ".env", suffix, names)
        rows.append(f"{index} EXOMEM_{suffix}")
    cases_file = tmp_path / "cases.txt"
    cases_file.write_text("\n".join(rows), encoding="utf-8")
    script = f"""
. '{COMMON_PS1}'
foreach ($row in Get-Content -LiteralPath '{cases_file}') {{
    $index, $name = $row -split ' '
    try {{
        $value = Get-ExomemDotenvValue -RepoRoot (Join-Path '{tmp_path}' "d$index") -Name $name
        "$index|ok|$value"
    }} catch {{
        "$index|refused|"
    }}
}}
"""
    out = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=300, check=False,
    )

    answers = {}
    for line in out.stdout.splitlines():
        index, status, value = line.split("|", 2)
        answers[int(index)] = (status, value)
    assert len(answers) == len(CASES), out.stderr
    for index, (suffix, names) in enumerate(CASES):
        status, value = answers[index]
        got = "refused" if status == "refused" else f"value:{value}"
        assert got == _expected(suffix, names), (suffix, names, status, value)


def _bind(tmp_path: Path, env_lines: list[str], preferred: Path | None) -> subprocess.CompletedProcess[str]:
    """Run the service scripts' state-root binder over a systemd unit whose env file holds `env_lines`."""
    env_file = tmp_path / "service.env"
    env_file.write_text("".join(f"{line}\n" for line in env_lines), encoding="utf-8")
    unit = tmp_path / "exomem.service"
    unit.write_text(f"[Service]\nEnvironmentFile={env_file}\n", encoding="utf-8")
    return subprocess.run(
        [
            BASH, "-c",
            f'set -euo pipefail; . "{COMMON_SH}"; '
            f'exomem_bind_service_state_root "{unit}" "{sys.executable}" "{preferred or ""}" bind',
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )


@needs_systemd_layout
def test_binding_a_state_root_never_writes_the_explicit_name_beside_a_differing_alias(tmp_path) -> None:
    """The failure: the binder saw no EXOMEM_STATE_ROOT, appended one, and the service exited 2 on the pair."""
    custom, preferred = tmp_path / "custom", tmp_path / "preferred"
    lines = [f"EVERMATTER_STATE_ROOT={custom}", f"EXOMEM_VAULT_PATH={tmp_path / 'vault'}"]

    result = _bind(tmp_path, lines, preferred)

    assert result.returncode != 0
    assert "does not match the durable transition receipt" in result.stderr
    assert (tmp_path / "service.env").read_text().splitlines() == lines


@needs_systemd_layout
def test_binding_a_state_root_keeps_the_name_the_env_file_uses(tmp_path) -> None:
    custom = tmp_path / "custom"
    result = _bind(tmp_path, [f"EVERMATTER_STATE_ROOT={custom}"], custom)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(custom)
    assert (tmp_path / "service.env").read_text() == f"EVERMATTER_STATE_ROOT={custom}\n"


@needs_systemd_layout
def test_binding_an_empty_state_root_fills_the_name_the_env_file_already_has(tmp_path) -> None:
    preferred = tmp_path / "preferred"
    result = _bind(tmp_path, ["EVERMATTER_STATE_ROOT="], preferred)

    assert result.returncode == 0, result.stderr
    written = (tmp_path / "service.env").read_text()
    assert written == f'EVERMATTER_STATE_ROOT="{preferred}"\n'
    assert "EXOMEM_STATE_ROOT" not in written


@needs_systemd_layout
def test_binding_refuses_two_state_roots_naming_both_variables_and_no_value(tmp_path) -> None:
    result = _bind(
        tmp_path, [f"EVERMATTER_STATE_ROOT={tmp_path / 'alias-root'}", f"EXOMEM_STATE_ROOT={tmp_path / 'explicit-root'}"],
        tmp_path / "explicit-root",
    )

    assert result.returncode != 0
    assert "EVERMATTER_STATE_ROOT and EXOMEM_STATE_ROOT" in result.stderr
    assert "alias-root" not in result.stderr and "explicit-root" not in result.stderr


@needs_pwsh
def test_the_powershell_binder_refuses_a_state_root_that_differs_from_the_file_and_writes_nothing(tmp_path) -> None:
    custom, preferred = tmp_path / "custom", tmp_path / "preferred"
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"EVERMATTER_STATE_ROOT={custom}\n", encoding="utf-8")
    script = f"""
. '{COMMON_PS1}'
try {{ Ensure-ExomemDotenvStateRootBinding -AppDirectory '{tmp_path}' -ExpectedStateRoot '{preferred}'; 'bound' }}
catch {{ 'refused: ' + $_.Exception.Message }}
"""
    out = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert out.stdout.startswith("refused: "), out.stdout + out.stderr
    assert "EXOMEM_STATE_ROOT" not in dotenv.read_text()


# A script that forces EXOMEM_X must leave no alias for it in the environment of the commands it
# starts: the package refuses a process that holds both names with different values.
CHILD = [sys.executable, "-m", "exomem.catalogue_prewarm"]  # exits 2 on the missing --vault, or 2 on a refusal
CHILD_RAN = "the following arguments are required: --vault"


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EXOMEM_", "EVERMATTER_", "KB_MCP_"))}
    env["PYTHONPATH"] = str(ROOT / "src")
    return env | extra


def _bash(tmp_path: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f'. "{COMMON_SH}"; {body}'],
        capture_output=True, text=True, timeout=120, check=False, cwd=tmp_path, env=_child_env(**env),
    )


@needs_bash
def test_a_command_started_after_a_forced_state_root_runs_despite_an_inherited_alias(tmp_path) -> None:
    """The failure: `export EXOMEM_STATE_ROOT=Y` left an inherited EVERMATTER_STATE_ROOT=X, and `exomem` exited 2."""
    inherited = {"EVERMATTER_STATE_ROOT": str(tmp_path / "inherited-alias")}
    child = " ".join(f'"{part}"' for part in CHILD)

    plain = _bash(tmp_path, f'export EXOMEM_STATE_ROOT="{tmp_path / "forced"}"; {child}', **inherited)
    forced = _bash(
        tmp_path,
        f'exomem_export_setting EXOMEM_STATE_ROOT "{tmp_path / "forced"}"; echo "root=$EXOMEM_STATE_ROOT"; {child}',
        **inherited,
    )

    assert plain.returncode == 2 and "not run" in plain.stderr  # the failure this guards against
    assert f"root={tmp_path / 'forced'}" in forced.stdout  # the forced value is the one in force
    assert CHILD_RAN in forced.stderr and "not run" not in forced.stderr


@needs_bash
def test_the_installers_process_file_loads_a_dotenv_alias_over_an_inherited_name(tmp_path) -> None:
    """The failure: sourcing the dotenv's EVERMATTER_VAULT_PATH beside an inherited EXOMEM_VAULT_PATH refused after publishing."""
    script = (ROOT / "scripts" / "install-service.sh").read_text(encoding="utf-8")
    (renderer,) = [
        block for block in re.findall(r"<<'PY'\n(.*?)\nPY\n", script, re.S) if "dotenv_values(env_path)" in block
    ]
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"EVERMATTER_VAULT_PATH={tmp_path / 'dotenv-vault'}\nEXOMEM_PORT=1\nKB_MCP_PORT=2\nOTHER_NAME=kept\n",
        encoding="utf-8",
    )
    staged, process_file = tmp_path / "service.env", tmp_path / "process.sh"
    rendered = subprocess.run(
        [sys.executable, "-", dotenv, staged, process_file, tmp_path / "launchd.xml", tmp_path / "logs"],
        input=renderer, capture_output=True, text=True, timeout=60, check=False,
        env=_child_env(EXOMEM_MANAGED_STATE_ROOT_DEFAULT=str(tmp_path / "state"), PATH=os.environ["PATH"]),
    )
    assert rendered.returncode == 0, rendered.stderr
    published = staged.read_text(encoding="utf-8")
    assert f'EVERMATTER_VAULT_PATH="{tmp_path / "dotenv-vault"}"' in published  # service.env keeps the dotenv's own name
    assert "EXOMEM_VAULT_PATH" not in published

    child = " ".join(f'"{part}"' for part in CHILD)
    loaded = _bash(
        tmp_path,
        f'. "{process_file}"; echo "vault=$(exomem_process_setting EXOMEM_VAULT_PATH)"; '
        f'echo "port=$EXOMEM_PORT other=$OTHER_NAME"; env | grep -c "^EVERMATTER_\\|^KB_MCP_" || true; {child}',
        EXOMEM_VAULT_PATH=str(tmp_path / "shell-vault"), EVERMATTER_STATE_ROOT=str(tmp_path / "inherited-alias"),
    )

    assert f"vault={tmp_path / 'dotenv-vault'}" in loaded.stdout  # the loaded value wins, with no refusal
    assert "port=1 other=kept" in loaded.stdout
    assert loaded.stdout.splitlines()[-1] == "0"  # every alias is gone
    assert CHILD_RAN in loaded.stderr and "not run" not in loaded.stderr


@needs_pwsh
def test_a_command_started_after_a_forced_state_root_runs_under_powershell_too(tmp_path) -> None:
    child = " ".join(f"'{part}'" for part in CHILD)
    script = f"""
. '{COMMON_PS1}'
$env:EVERMATTER_STATE_ROOT = '{tmp_path / "inherited-alias"}'
[Environment]::SetEnvironmentVariable('EXOMEM_STATE_ROOT', '{tmp_path / "forced"}', 'Process')
'plain: ' + ((& {child} 2>&1) | Out-String)
Set-ExomemProcessSetting -Name 'EXOMEM_STATE_ROOT' -Value '{tmp_path / "forced"}'
'forced: root=' + $env:EXOMEM_STATE_ROOT + ' ' + ((& {child} 2>&1) | Out-String)
$env:EXOMEM_VAULT_PATH = '{tmp_path / "shell-vault"}'
Set-ExomemProcessEnvFromMap -Map ([ordered]@{{ EVERMATTER_VAULT_PATH = '{tmp_path / "dotenv-vault"}'; OTHER_NAME = 'kept' }})
'loaded: ' + $env:EXOMEM_VAULT_PATH + ' ' + $env:OTHER_NAME + ' ' + [string]($null -eq $env:EVERMATTER_VAULT_PATH)
"""
    out = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False, cwd=tmp_path, env=_child_env(),
    )

    plain, _, rest = out.stdout.partition("forced: ")
    forced, _, loaded = rest.partition("loaded: ")
    assert "not run" in plain, out.stdout + out.stderr  # the failure this guards against
    assert forced.startswith(f"root={tmp_path / 'forced'} ")  # the forced value is the one in force
    assert CHILD_RAN in forced and "not run" not in forced
    assert loaded.strip() == f"{tmp_path / 'dotenv-vault'} kept True"


def test_no_service_script_forces_an_exomem_name_without_the_helper() -> None:
    """The class guard: a forced EXOMEM_ name that skips the helper leaves an inherited alias beside it."""
    forced = re.compile(
        r"""^\s*export\s+EXOMEM_|SetEnvironmentVariable\(\s*["']EXOMEM_|\$env:EXOMEM_\w+\s*=""", re.MULTILINE
    )
    offenders = [
        path.name
        for path in sorted((ROOT / "scripts").glob("*.sh")) + sorted((ROOT / "scripts").glob("*.ps1"))
        if forced.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
