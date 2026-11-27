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
import shlex
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


def test_a_hook_home_conflict_skips_a_turn_only_when_no_flag_decides_the_home() -> None:
    """The failure: a hook told its home by a flag still skipped every turn over an unused HOOK_HOME conflict."""
    from exomem._hooks import (
        exomem_capture_nudge,
        exomem_continuation_checkpoint,
        exomem_retrieve_nudge,
    )

    for hook in (exomem_capture_nudge, exomem_retrieve_nudge, exomem_continuation_checkpoint):
        assert hook._skips_turn(["EVERMATTER_HOOK_HOME"], home_decided=False), hook.__name__
        assert not hook._skips_turn(["EVERMATTER_HOOK_HOME"], home_decided=True), hook.__name__
        # A flag decides the hook's home only; a conflict in the vault still skips the turn.
        assert hook._skips_turn(["EVERMATTER_HOOK_HOME", "EVERMATTER_VAULT_PATH"], home_decided=True), hook.__name__
        assert not hook._skips_turn(["EVERMATTER_PORT"], home_decided=False), hook.__name__


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
    # A file, not one `bash -c` argument: the matrix outgrows the kernel's per-argument limit (E2BIG).
    script = tmp_path / "matrix.sh"
    script.write_text("\n".join(lines), encoding="utf-8")
    out = subprocess.run([BASH, str(script)], capture_output=True, text=True, timeout=300, check=False)

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
    # The regex only cuts the installer's fixed <<'PY' heredocs out of the script; it reads no shell syntax,
    # and the one-element unpacking fails if the renderer block is ever missing or duplicated.
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


# The writers publish EVERMATTER_X and never two names for one setting. Each test below fails on the
# writer that still emitted EXOMEM_X, or kept the file's other spellings beside the one it wrote.


def _renderer_source() -> str:
    script = (ROOT / "scripts" / "install-service.sh").read_text(encoding="utf-8")
    # The regex cuts the installer's fixed <<'PY' heredocs out of the script; it reads no shell syntax.
    (renderer,) = [
        block for block in re.findall(r"<<'PY'\n(.*?)\nPY\n", script, re.S) if "dotenv_values(env_path)" in block
    ]
    return renderer


@needs_bash
def test_the_installer_publishes_each_setting_once_as_evermatter_and_keeps_the_process_file_internal(tmp_path) -> None:
    """The failure: a legacy-named dotenv was published under its legacy names, in service.env and the launchd plist."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"EXOMEM_VAULT_PATH={tmp_path / 'vault'}\nKB_MCP_PORT=2\nEVERMATTER_PORT=3\nEXOMEM_LOG_DIR={tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    staged, process_file, plist = tmp_path / "service.env", tmp_path / "process.sh", tmp_path / "launchd.xml"
    rendered = subprocess.run(
        [sys.executable, "-", dotenv, staged, process_file, plist, tmp_path / "default-logs"],
        input=_renderer_source(), capture_output=True, text=True, timeout=60, check=False,
        env=_child_env(EXOMEM_MANAGED_STATE_ROOT_DEFAULT=str(tmp_path / "state"), PATH=os.environ["PATH"]),
    )

    assert rendered.returncode == 0, rendered.stderr
    for published in (staged.read_text(encoding="utf-8"), plist.read_text(encoding="utf-8")):
        assert str(tmp_path / "vault") in published and str(tmp_path / "state") in published
        assert published.count("EVERMATTER_PORT") == 1 and "3" in published  # EVERMATTER_PORT outranks KB_MCP_PORT
        assert "EXOMEM_" not in published and "KB_MCP_" not in published
    assert "EVERMATTER_VAULT_PATH=" in staged.read_text(encoding="utf-8")
    # The process file is deleted by the installer after its own shell sources it, so it keeps the internal name.
    assert f"exomem_export_setting EXOMEM_VAULT_PATH {shlex.quote(str(tmp_path / 'vault'))}\n" in process_file.read_text(
        encoding="utf-8"
    )


@needs_systemd_layout
def test_binding_a_fresh_state_root_writes_the_public_name_and_leaves_no_other_spelling(tmp_path) -> None:
    """The failure: the binder appended EXOMEM_STATE_ROOT, or filled an empty legacy line in place under its legacy name."""
    preferred = tmp_path / "preferred"

    appended = _bind(tmp_path, ["OTHER=kept"], preferred)
    assert appended.returncode == 0, appended.stderr
    assert (tmp_path / "service.env").read_text() == f'OTHER=kept\nEVERMATTER_STATE_ROOT="{preferred}"\n'

    filled = _bind(tmp_path, ["EXOMEM_STATE_ROOT=", "OTHER=kept", "KB_MCP_STATE_ROOT="], preferred)
    assert filled.returncode == 0, filled.stderr
    assert (tmp_path / "service.env").read_text() == f'EVERMATTER_STATE_ROOT="{preferred}"\nOTHER=kept\n'


@needs_bash
def test_a_hosted_state_root_conflict_is_refused_before_anything_stops(tmp_path) -> None:
    """The failure: only the first process that read the hosted state root refused, after the service was down."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("EVERMATTER_HOSTED_STATE_ROOT=/one\nEXOMEM_HOSTED_STATE_ROOT=/two\n", encoding="utf-8")
    agreeing = tmp_path / "agreeing.env"
    agreeing.write_text("EVERMATTER_HOSTED_STATE_ROOT=/one\nEXOMEM_HOSTED_STATE_ROOT=/one\n", encoding="utf-8")

    refused = _bash(tmp_path, f'exomem_refuse_hosted_state_root_conflicts "{dotenv}"')
    accepted = _bash(tmp_path, f'exomem_refuse_hosted_state_root_conflicts "{agreeing}" "{tmp_path / "absent.env"}"')

    assert refused.returncode != 0
    assert "EVERMATTER_HOSTED_STATE_ROOT and EXOMEM_HOSTED_STATE_ROOT" in refused.stderr
    assert "/one" not in refused.stderr and "/two" not in refused.stderr
    assert accepted.returncode == 0, accepted.stderr


@needs_bash
def test_the_shell_dotenv_reader_takes_the_last_duplicate_like_python_dotenv(tmp_path) -> None:
    from dotenv import dotenv_values

    dotenv = tmp_path / ".env"
    dotenv.write_text("EXOMEM_PORT=1\nEVERMATTER_PORT=7\nEXOMEM_PORT=2\n", encoding="utf-8")

    read = _bash(tmp_path, f'exomem_dotenv_file_lookup EXOMEM_PORT "{dotenv}"')

    assert read.stdout.strip() == dotenv_values(dotenv)["EXOMEM_PORT"] == "2"


def test_the_hook_reads_the_last_duplicate_like_python_dotenv(tmp_path) -> None:
    """The failure: the hook sent the first REST key in service.env while the service read the last one."""
    from dotenv import dotenv_values

    from exomem._hooks import exomem_local_credentials

    service_env = tmp_path / "service.env"
    service_env.write_text("EVERMATTER_REST_API_KEY=old\nOTHER=x\nEVERMATTER_REST_API_KEY=new\n", encoding="utf-8")

    read = exomem_local_credentials.service_env_value("EXOMEM_REST_API_KEY", service_env)

    assert read == dotenv_values(service_env)["EVERMATTER_REST_API_KEY"] == "new"


@needs_pwsh
def test_the_powershell_writers_publish_evermatter_names_and_read_the_last_duplicate(tmp_path) -> None:
    """One pwsh run: the binder, the published-settings map, the duplicate reader and the hosted-state-root refusal."""
    bind_dir, dup_dir, host_dir = (tmp_path / name for name in ("bind", "dup", "host"))
    for directory in (bind_dir, dup_dir, host_dir):
        directory.mkdir()
    preferred = tmp_path / "preferred"
    (bind_dir / ".env").write_text("EXOMEM_STATE_ROOT=\nKB_MCP_STATE_ROOT=\nOTHER=kept\n", encoding="utf-8")
    (dup_dir / ".env").write_text("EXOMEM_PORT=1\nEXOMEM_PORT=2\n", encoding="utf-8")
    (host_dir / ".env").write_text(
        "EVERMATTER_HOSTED_STATE_ROOT=/one\nEXOMEM_HOSTED_STATE_ROOT=/two\n", encoding="utf-8"
    )
    script = f"""
. '{COMMON_PS1}'
Ensure-ExomemDotenvStateRootBinding -AppDirectory '{bind_dir}' -ExpectedStateRoot '{preferred}' | Out-Null
'bound: ' + ((Get-Content -Raw -LiteralPath '{bind_dir / ".env"}') -replace "`r?`n", '|')
'dup: ' + (Get-ExomemDotenvValue -RepoRoot '{dup_dir}' -Name 'EXOMEM_PORT')
$map = [ordered]@{{ EXOMEM_VAULT_PATH = '/v'; KB_MCP_PORT = '2'; EVERMATTER_PORT = '3'; OTHER = 'kept' }}
$published = ConvertTo-ExomemPublishedSettings -Map $map
'published: ' + (($published.Keys | ForEach-Object {{ "$_=$($published[$_])" }}) -join ',')
try {{ Assert-ExomemHostedStateRootUnambiguous -AppDirectory '{host_dir}'; 'hosted: accepted' }}
catch {{ 'hosted: ' + $_.Exception.Message }}
"""
    out = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False, env=_child_env(),
    )

    lines = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in out.stdout.splitlines() if ": " in line}
    assert lines["bound"] == f"EVERMATTER_STATE_ROOT={preferred}|OTHER=kept|", out.stdout + out.stderr
    assert lines["dup"] == "2"
    assert lines["published"] == "EVERMATTER_VAULT_PATH=/v,EVERMATTER_PORT=3,OTHER=kept"
    assert "EVERMATTER_HOSTED_STATE_ROOT and EXOMEM_HOSTED_STATE_ROOT" in lines["hosted"]
    assert "/one" not in lines["hosted"] and "/two" not in lines["hosted"]


@needs_pwsh
def test_the_nssm_environment_merge_replaces_an_entry_stored_under_another_spelling(tmp_path) -> None:
    """The failure: an upgrade kept the old install's EXOMEM_X entry beside the new EVERMATTER_X one, so the service held two."""
    installer = ROOT / "scripts" / "install-service.ps1"
    script = f"""
. '{COMMON_PS1}'
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{installer}', [ref]$null, [ref]$null)
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Set-NssmEnvironment' }}, $true) | Select-Object -First 1
. ([scriptblock]::Create($function.Extent.Text))
function Get-ItemProperty {{
    param($Path, $ErrorAction)
    [pscustomobject]@{{ AppEnvironmentExtra = @('EXOMEM_PORT=1', 'KB_MCP_BIND_HOST=old', 'OPAQUE=kept', 'EXOMEM_LOG_DIR=x') }}
}}
function Invoke-FakeNssm {{ $global:LASTEXITCODE = 0; $global:Captured = $args }}
$NssmPath = 'Invoke-FakeNssm'
$ServiceName = 'exomem'
Set-NssmEnvironment -Map ([ordered]@{{ EVERMATTER_PORT = '2'; EVERMATTER_BIND_HOST = '127.0.0.1' }})
'args: ' + ($global:Captured -join '|')
"""
    out = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False, env=_child_env(),
    )

    stored = next(line for line in out.stdout.splitlines() if line.startswith("args: ")).split("|")[3:]
    assert sorted(stored) == ["EVERMATTER_BIND_HOST=127.0.0.1", "EVERMATTER_PORT=2", "EXOMEM_LOG_DIR=x", "OPAQUE=kept"], (
        out.stdout + out.stderr
    )
