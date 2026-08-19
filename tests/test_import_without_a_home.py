"""Importing the product must not require a home directory.

`Path.home()` raises `RuntimeError` outright when the environment names no
home. That is the ordinary state of a Windows service account, a container
started without `HOME`, and any child process handed a minimal environment --
and three modules called it while their module bodies ran, so `import
exomem.commands` failed there and the entire command surface was unreachable
for want of a directory nothing had asked for yet.

These pin the invariant rather than the platform: an import may not resolve a
home directory, on any operating system. Written as a subprocess with
`Path.home` replaced by a raising stub, because the failure is an import-time
one and an already-imported module cannot show it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Every module that sits between an entry point and the command surface. The
# import chain that actually broke ran commands -> delete_directory ->
# governance.lifecycle -> governance.receipts -> writer_lease, and failed on
# the last one's dataclass field default.
_MODULES = (
    "exomem",
    "exomem.__main__",
    "exomem.commands",
    "exomem.server",
    "exomem.writer_lease",
    "exomem.install_hook",
    "exomem.install_skill",
)

_DENY_HOME = (
    "import pathlib\n"
    "def _no_home(cls):\n"
    "    raise RuntimeError('Could not determine home directory.')\n"
    "pathlib.Path.home = classmethod(_no_home)\n"
)


def _run_without_a_home(body: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in sys.path if path and Path(path).is_dir()
    )
    return subprocess.run(
        [sys.executable, "-c", _DENY_HOME + body],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("module", _MODULES)
def test_the_module_imports_where_there_is_no_home_directory(module: str) -> None:
    result = _run_without_a_home(f"import {module}\nprint('imported')\n")

    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


def test_a_lease_told_where_its_state_lives_needs_no_home() -> None:
    """The caller that supplies `state_dir` is every deployed one.

    The hosted cell and the installed service both pass their own path, so the
    default this used to evaluate at import was never the value they used --
    it only had to exist for the class body to finish.
    """
    result = _run_without_a_home(
        "from pathlib import PurePosixPath\n"
        "from exomem.writer_lease import LeaseConfig\n"
        "config = LeaseConfig(state_dir=PurePosixPath('/srv/exomem/state'))\n"
        "print(config.state_dir)\n"
    )

    assert result.returncode == 0, result.stderr
    assert "/srv/exomem/state" in result.stdout


def test_asking_for_the_default_without_a_home_still_reports_the_real_reason() -> None:
    """Deferring the call moves the failure; it does not swallow it.

    A caller that genuinely wants the home-relative default and has no home
    still gets `RuntimeError` -- now at the point of asking, where it names
    something the caller can act on, instead of at import of an unrelated
    module.
    """
    result = _run_without_a_home(
        "from exomem.writer_lease import LeaseConfig\n"
        "try:\n"
        "    LeaseConfig()\n"
        "except RuntimeError as error:\n"
        "    print('refused:', error)\n"
    )

    assert result.returncode == 0, result.stderr
    assert "refused:" in result.stdout


def test_the_deferred_names_still_resolve_where_there_is_a_home() -> None:
    """The values callers read are unchanged; only when they resolve moved."""
    from exomem import install_hook, install_skill

    home = Path.home()

    assert install_hook.DEFAULT_CLAUDE_HOOK_DIR == home / ".claude" / "hooks"
    assert install_hook.DEFAULT_CLAUDE_SETTINGS == home / ".claude" / "settings.json"
    assert install_hook.DEFAULT_CODEX_HOOK_DIR == home / ".codex" / "hooks"
    assert install_hook.DEFAULT_CODEX_SETTINGS == home / ".codex" / "hooks.json"
    assert install_hook.DEFAULT_HOOK_DIR == home / ".claude" / "hooks"
    assert install_hook.DEFAULT_SETTINGS == home / ".claude" / "settings.json"
    assert install_skill.DEFAULT_TARGET == home / ".claude" / "skills" / "exomem"
    assert (
        install_skill._LEGACY_TARGET == home / ".claude" / "skills" / "knowledge-base"
    )


def test_an_unknown_name_is_still_an_attribute_error() -> None:
    """A module `__getattr__` must not turn every typo into a Path."""
    from exomem import install_hook, install_skill

    with pytest.raises(AttributeError):
        install_hook.DEFAULT_NOTHING_DIR
    with pytest.raises(AttributeError):
        install_skill.DEFAULT_NOTHING_DIR
