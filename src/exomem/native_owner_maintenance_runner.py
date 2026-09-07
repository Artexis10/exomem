"""Run the canonical owner-maintenance shell helper from an installed package."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from . import held_fs

_HELPERS = ("owner-setup.sh", "_service-common.sh", "service-transition-receipt.py")


def require_supported_platform() -> None:
    """Refuse maintenance where its held-filesystem safety boundary is absent."""

    support = held_fs.platform_support()
    if not support.supported:
        raise SystemExit(f"Owner maintenance is unavailable: {support.reason}")
    if os.name == "nt":
        raise SystemExit("Owner maintenance is unavailable on Windows")


def _script_path() -> Path:
    module = Path(__file__).resolve()
    directory = module.parent / "_service"
    if not directory.exists() and module.parent.parent.name == "src":
        root = module.parent.parent.parent
        if (root / "pyproject.toml").is_file():
            directory = root / "scripts"
    if not all((directory / name).is_file() for name in _HELPERS):
        raise SystemExit("Owner maintenance helpers are missing from this installation")
    return directory / "owner-setup.sh"


def main(argv: Sequence[str] | None = None) -> None:
    require_supported_platform()
    bash = shutil.which("bash")
    if bash is None:
        raise SystemExit("Owner maintenance requires bash")
    arguments = list(sys.argv[1:] if argv is None else argv)
    script = _script_path()
    try:
        os.execv(bash, [bash, str(script), *arguments])
    except OSError:
        raise SystemExit("Could not start the owner maintenance helper") from None


if __name__ == "__main__":
    main()
