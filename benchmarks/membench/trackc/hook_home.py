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

_BASE_PATH = "/usr/bin:/bin"


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
            "PATH": _BASE_PATH,
            "HOME": str(self.base),
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
    env = {
        "PATH": _BASE_PATH,
        "HOME": str(base),
        "EXOMEM_HOOK_HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home),
        "CODEX_HOME": str(home),
        "PYTHONPATH": str(SRC_DIR),
        "PYTHONUTF8": "1",
    }
    if os.name == "nt":
        # Windows resolves Winsock through `%SystemRoot%`, and `exomem.__main__`
        # imports `asyncio`, whose Windows event loop pulls that in at import
        # time. A replacement environment without it does not merely lose a
        # convenience: the interpreter cannot start the program at all, exiting
        # with `OSError [WinError 10106] The requested service provider could
        # not be loaded or initialized` before any exomem code runs. Isolation
        # is unaffected -- this names the OS install, not the user's state,
        # which the entries above still redirect.
        system_root = os.environ.get("SystemRoot")
        if system_root:
            env["SystemRoot"] = system_root
        # `HOME` alone redirects the home directory on POSIX only. Windows
        # resolves `Path.home()` through `USERPROFILE`, falling back to
        # `HOMEDRIVE`+`HOMEPATH`, so without these the isolation is not merely
        # incomplete -- `install_hook` computes a default hook directory from
        # `Path.home()` at import scope, which raises "Could not determine home
        # directory" and the subprocess dies before it can install anything.
        env["USERPROFILE"] = str(base)
        drive, tail = os.path.splitdrive(str(base))
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail
    return env


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
    hooks_dir = str(home.hooks_dir)
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
