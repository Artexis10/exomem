"""Isolated hook homes + ``exomem install-hook`` wiring assertions (Track C).

Installs the shipped hooks into a benchmark-owned home exactly the way a user
would (``python -m exomem install-hook --client claude|codex`` as a
subprocess), with FULL isolation via the installer's documented env knobs
(src/exomem/install_hook.py lines 99-106: EXOMEM_HOOK_HOME wins over
CLAUDE_CONFIG_DIR / CODEX_HOME, which win over ``~``). HOME is additionally
pointed at the benchmark base so no code path can reach the real
``~/.claude`` / ``~/.codex``.

Trusted temp root: the installer and the continuation checkpoint refuse
directories whose ancestor chain is writable by another principal — on this
host ``/tmp`` is sticky but owned by ``nobody``, which the walk in
src/exomem/_hooks/exomem_continuation_checkpoint.py lines 1350-1379 rejects
(sticky is only trusted for root/self owners, line 1370-1373). Hook homes are
therefore allocated under ``<repo>/.pytest-tmp`` (gitignored), whose ancestor
chain is owner-writable only.

Wiring contract asserted here (src/exomem/install_hook.py):
- claude: config file is ``<home>/settings.json`` (lines 94-96); nudge entries
  are ``bash "<hook_dir>/exomem-capture-nudge.sh"`` (Stop) and
  ``bash "<hook_dir>/exomem-retrieve-nudge.sh"`` (UserPromptSubmit) for a
  custom hook dir (lines 135-137); continuation entries are
  ``bash "<hook_dir>/exomem-continuation-checkpoint.sh" --client claude`` for
  PreCompact (matcher "manual|auto"), SessionEnd (no matcher), SessionStart
  (matcher "compact|resume") (lines 44-49, 261-293).
- codex: config file is ``<home>/hooks.json``; commands run the bundled Python
  scripts directly (``python3 "<hook_dir>/<script>.py>"``, lines 130-134) and
  the continuation command carries ``--client codex``; codex has NO SessionEnd
  (lines 50-53).
- entries are ``{"type": "command", "command": ..., "timeout": ...}`` groups
  under ``hooks.<Event>[].hooks[]`` (lines 151-159, 1129-1172).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

#: Events the installer wires per client (install_hook.py lines 34-37, 44-54).
CLAUDE_EVENTS = ("Stop", "UserPromptSubmit", "PreCompact", "SessionEnd", "SessionStart")
CODEX_EVENTS = ("Stop", "UserPromptSubmit", "PreCompact", "SessionStart")

_POSIX_PATH = "/usr/bin:/bin"

#: Variables Windows itself needs in a child, as opposed to anything about the
#: user's configuration. `SystemRoot` is the load-bearing one: Winsock resolves
#: its service providers underneath it, so a child without it dies importing
#: `asyncio` -- `_overlapped` raises `WinError 10106, the requested service
#: provider could not be loaded or initialized` before any exomem code runs. On
#: the Windows lane that took out 14 tests across tracks C and D, every one of
#: them reported as a setup failure of `install-hook` rather than as the
#: environment defect it is. The rest are the ordinary plumbing a spawned
#: process expects: where to find the shell and executable suffixes, where to
#: put temporary files, and the CPU facts some runtimes read at startup.
_WINDOWS_PASSTHROUGH = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "TEMP",
    "TMP",
    "PATHEXT",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def home_env(base: Path) -> dict[str, str]:
    """Point the platform's own idea of the home directory at `base`.

    `HOME` alone redirects nothing on Windows: `Path.home()` and `~` are read
    from `USERPROFILE`, falling back to `HOMEDRIVE` plus `HOMEPATH`, and with
    none of them set the interpreter raises rather than guessing. So a child
    given only `HOME` did not get an isolated home there -- it got no home at
    all, which is a different and much louder failure than leaking into the
    real one.

    All the spellings are set together so the child sees one answer whichever
    it consults, and so a hook that resolves `~` lands in the same isolated
    tree the harness is asserting against.
    """
    home = {"HOME": str(base)}
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(base))
        home["USERPROFILE"] = str(base)
        home["HOMEDRIVE"] = drive
        home["HOMEPATH"] = tail
    return home


def platform_env() -> dict[str, str]:
    """OS plumbing for an isolated child, and nothing that carries user state.

    The isolation these harnesses want is from the *user's* configuration --
    their real client homes, their exomem environment -- not from the operating
    system. A hardcoded POSIX `PATH` provided both on Linux and neither on
    Windows, where it named directories that do not exist and left out the
    variables the platform requires.

    Nothing here is read from exomem's own environment, so what the harness
    isolates is unchanged; `ensure_isolated` still governs that and still sees
    exactly the keys it did before.
    """
    if os.name != "nt":
        return {"PATH": _POSIX_PATH}
    carried = {
        name: os.environ[name] for name in _WINDOWS_PASSTHROUGH if name in os.environ
    }
    system_root = carried.get("SystemRoot") or carried.get("windir") or ""
    # Resolved tools first; the system directories are fallback plumbing.
    directories = [
        *_tool_directories(),
        str(Path(system_root) / "System32"),
        system_root,
        str(Path(system_root) / "System32" / "Wbem"),
    ]
    carried["PATH"] = os.pathsep.join(dict.fromkeys(item for item in directories if item))
    return carried


#: The Microsoft Store publishes stub executables under this directory:
#: `python3.exe` there is an installer prompt, not an interpreter, and a hook
#: that reaches one gets `NoInstallsError: No runtimes are installed` instead of
#: running.
_STORE_ALIASES = "windowsapps"


def bash_executable() -> str:
    """An absolute path to a Windows-native bash, never the WSL launcher.

    Naming it matters because PATH does not settle this on Windows.
    `subprocess` hands a bare program name to `CreateProcess`, which searches
    the system directory *before* any PATH entry -- so wherever WSL is
    installed, `bash` is the launcher in System32, and that one cannot open the
    `C:` path the wrapper is invoked with. The hook exits 127 and the suite
    scores a missing interpreter as a hook that chose not to fire, which is the
    same reading with none of the truth in it.

    Returns the bare name where nothing better is found, so a host without a
    git-bash fails the way it did rather than differently.
    """
    if os.name != "nt":
        return "bash"
    resolved = shutil.which("bash")
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
    shim = None
    if system_root:
        shim = str(Path(system_root) / "System32" / "bash.exe").casefold()
    if resolved is not None and (shim is None or resolved.casefold() != shim):
        return resolved
    git = shutil.which("git")
    if git is not None:
        # Git for Windows keeps bash in a sibling tree of the exe it advertises:
        # `<root>/mingw64/bin/git.exe` alongside `<root>/usr/bin/bash.exe`.
        for parent in Path(git).parents:
            for relative in (("usr", "bin", "bash.exe"), ("bin", "bash.exe")):
                candidate = parent.joinpath(*relative)
                if candidate.is_file():
                    return str(candidate)
    return resolved or "bash"


def _tool_directories() -> list[str]:
    """Where the programs a wired hook command names actually live.

    The Claude wrappers are invoked as `bash ~/.claude/hooks/<name>.sh` and then
    resolve an interpreter themselves, so a child that cannot find a real bash
    and a real Python does not run a hook at all -- it exits non-zero, and the
    suite reads that as the hook declining to fire rather than as a broken
    environment.

    The interpreter comes first and is `sys.executable`, so the hooks run under
    the same Python as the suite asserting about them rather than whichever
    build is earliest on this machine's PATH. Resolved by directory rather than
    by inheriting PATH: what these harnesses isolate is the user's
    configuration, and a toolchain is not that.
    """
    # The base interpreter as well as the running one. They are the same
    # directory outside a virtual environment; inside one they are not, and the
    # difference matters to the injection ladder, which needs a PATH that
    # resolves a Python but not this checkout's installed `exomem` console
    # script -- a distinction that does not exist on POSIX, where the base PATH
    # never held either.
    candidates = [
        Path(sys.executable).parent,
        Path(sys.base_prefix),
        Path(bash_executable()).parent,
    ]
    git = shutil.which("git")
    if git is not None:
        candidates.append(Path(git).parent)
    return [
        str(item) for item in candidates if item.name.casefold() != _STORE_ALIASES
    ]


def trusted_tmp_root() -> Path:
    """Benchmark temp root whose ancestor chain passes the hooks' trust walk."""
    root = REPO_ROOT / ".pytest-tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_workdir(prefix: str) -> Path:
    """A fresh 0o700 scratch dir under the trusted root (caller cleans up)."""
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=trusted_tmp_root()))


@dataclass
class HookHome:
    """One isolated installed hook home."""

    client: str
    base: Path  # benchmark-owned parent (also used as HOME)
    home: Path  # the hook home (EXOMEM_HOOK_HOME + client config dir)
    install_report: dict = field(default_factory=dict)

    @property
    def hooks_dir(self) -> Path:
        return self.home / "hooks"

    @property
    def settings_path(self) -> Path:
        # install_hook.py lines 94-96: codex config is hooks.json.
        return self.home / ("hooks.json" if self.client == "codex" else "settings.json")

    def settings(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def base_env(self, **extra: str) -> dict[str, str]:
        """Minimal isolated env for running the installed hooks as the client
        would: nothing inherited from the test process, HOME redirected, all
        three isolation knobs pointed at this home."""
        env = {
            **platform_env(),
            **home_env(self.base),
            "EXOMEM_HOOK_HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.home),
            "CODEX_HOME": str(self.home),
            "PYTHONUTF8": "1",
        }
        env.update(extra)
        return env

    def wired_command(self, event: str, *, index: int = 0) -> str:
        groups = self.settings()["hooks"][event]
        return groups[index]["hooks"][0]["command"]


def install_env(base: Path, home: Path) -> dict[str, str]:
    """Env for the ``install-hook`` subprocess (isolated + this worktree's code)."""
    return {
        **platform_env(),
        **home_env(base),
        "EXOMEM_HOOK_HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home),
        "CODEX_HOME": str(home),
        "PYTHONPATH": str(SRC_DIR),
        "PYTHONUTF8": "1",
    }


def create_hook_home(
    client: str = "claude",
    *,
    base: Path | None = None,
    home: Path | None = None,
) -> HookHome:
    """Create an isolated home and install the shipped hooks into it.

    Runs ``python -m exomem install-hook --client <client> --json`` as a
    subprocess with env-injected isolation, exactly like a user install.
    Passing an explicit ``home`` lets two clients share one EXOMEM_HOOK_HOME
    (the cross-client checkpoint scenario).
    """
    if client not in {"claude", "codex"}:
        raise ValueError(f"unsupported client {client!r}")
    if base is None:
        base = make_workdir(f"hookhome-{client}")
    if home is None:
        home = base / "hook-home"
    home.mkdir(mode=0o700, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "exomem", "install-hook", "--client", client, "--json"],
        cwd=REPO_ROOT,
        env=install_env(base, home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"install-hook --client {client} failed rc={proc.returncode}: "
            f"{proc.stderr.strip()[:500]}"
        )
    report = json.loads(proc.stdout)
    return HookHome(client=client, base=base, home=home, install_report=report)


def _entries(settings: dict, event: str) -> list[dict]:
    out: list[dict] = []
    for group in settings.get("hooks", {}).get(event, []):
        out.extend(group.get("hooks", []))
    return out


def _wiring_problems(home: HookHome, events: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    if not home.settings_path.is_file():
        return [f"missing hook config {home.settings_path}"]
    settings = home.settings()
    # The POSIX spelling, because that is what the installer writes: a custom
    # hook directory goes in as a POSIX absolute path so the same command runs
    # under the `bash` the wrapper needs (install_hook.py `_command_for`).
    # Comparing against the native spelling made every wired entry look as if it
    # pointed outside the isolated home on Windows, on a separator alone.
    hooks_dir = home.hooks_dir.as_posix()
    for event in events:
        entries = _entries(settings, event)
        if not entries:
            problems.append(f"{event}: no wired entries")
            continue
        entry = entries[0]
        command = str(entry.get("command", ""))
        if entry.get("type") != "command":
            problems.append(f"{event}: entry type is {entry.get('type')!r}")
        if not isinstance(entry.get("timeout"), int):
            problems.append(f"{event}: missing integer timeout")
        if hooks_dir not in command:
            problems.append(f"{event}: command {command!r} not inside isolated {hooks_dir}")
        if event in {"PreCompact", "SessionEnd", "SessionStart"}:
            if f"--client {home.client}" not in command:
                problems.append(f"{event}: continuation command lacks --client {home.client}")
        if home.client == "codex" and not command.startswith("python3 "):
            problems.append(f"{event}: codex command should run python3 directly")
        if home.client == "claude" and not command.startswith("bash "):
            problems.append(f"{event}: claude command should run the bash wrapper")
    # deployed files exist
    expected_files = [
        "exomem_capture_nudge.py",
        "exomem-capture-nudge.sh",
        "exomem_retrieve_nudge.py",
        "exomem-retrieve-nudge.sh",
        "exomem_continuation_checkpoint.py",
        "exomem-continuation-checkpoint.sh",
    ]
    for name in expected_files:
        if not (home.hooks_dir / name).is_file():
            problems.append(f"deployed hook file missing: {name}")
    return problems


def verify_claude_wiring(home: HookHome) -> list[str]:
    """Assert claude settings.json wiring; returns [] when fully wired."""
    problems = _wiring_problems(home, CLAUDE_EVENTS)
    matchers = {
        "PreCompact": "manual|auto",
        "SessionStart": "compact|resume",
    }  # install_hook.py lines 44-49
    if home.settings_path.is_file():
        settings = home.settings()
        for event, matcher in matchers.items():
            groups = settings.get("hooks", {}).get(event, [])
            if not any(group.get("matcher") == matcher for group in groups):
                problems.append(f"{event}: expected matcher {matcher!r}")
    return problems


def verify_codex_wiring(home: HookHome) -> list[str]:
    """Assert codex hooks.json wiring; returns [] when fully wired."""
    problems = _wiring_problems(home, CODEX_EVENTS)
    if home.settings_path.is_file():
        settings = home.settings()
        # Pinned Codex 0.144.3 has no SessionEnd (install_hook.py lines 50-54,
        # 869-885): the installer must not wire one.
        if _entries(settings, "SessionEnd"):
            problems.append("SessionEnd: codex must not wire SessionEnd")
        for event in CODEX_EVENTS:
            for entry in _entries(settings, event):
                if "commandWindows" not in entry:
                    problems.append(f"{event}: codex entry lacks commandWindows")
                break
    return problems


def assert_wired(home: HookHome) -> None:
    problems = (
        verify_codex_wiring(home) if home.client == "codex" else verify_claude_wiring(home)
    )
    if problems:
        raise AssertionError(f"{home.client} wiring problems: {problems}")


def cleanup_workdir(base: Path) -> None:
    """Best-effort removal of a benchmark workdir (never a real home)."""
    import shutil

    base = Path(base)
    if trusted_tmp_root() not in base.parents:
        raise ValueError(f"refusing to remove non-benchmark dir {base}")
    shutil.rmtree(base, ignore_errors=True)


def ensure_isolated(env: dict[str, str]) -> None:
    """Guardrail: refuse env pointing at the user's REAL client homes.

    (Benchmark dirs legitimately live under the repo, which is under ~; the
    forbidden targets are ~ itself and the real ~/.claude / ~/.codex trees.)
    """
    real_home = Path(os.path.expanduser("~"))
    forbidden_exact = {str(real_home), str(real_home / ".claude"), str(real_home / ".codex")}
    repo_prefix = str(REPO_ROOT) + os.sep
    for key in ("EXOMEM_HOOK_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "EXOMEM_VAULT_PATH"):
        value = env.get(key)
        if not value:
            continue
        resolved = str(Path(value).expanduser())
        if resolved in forbidden_exact:
            raise AssertionError(f"{key}={value} points at a real client home")
        # This worktree itself may live under ~/.claude/worktrees/; anything
        # inside the repo (e.g. .pytest-tmp scratch) is benchmark-owned.
        if resolved.startswith(repo_prefix):
            continue
        for real_client_dir in (str(real_home / ".claude"), str(real_home / ".codex")):
            if resolved.startswith(real_client_dir + os.sep):
                raise AssertionError(f"{key}={value} points inside a real client home")
