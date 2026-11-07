"""Runs inside one arm's environment (`PYTHONPATH=<arm checkout>/src`): lays the arm's own starter scaffold in an empty
vault, copies a case's seeded pages into it, and readies the vault's external state.

    python session_seed.py <seed dir> <vault> <product root>

The caller pins every state root and the vault path in the environment (`session_close.pinned_env`), so this process
reaches no real state. The scaffold comes from the arm's `init_vault`, so each arm's vault carries that arm's own
`_Schema` and registries; the pages are the same files for both arms.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main(seed: Path, vault: Path, product_root: Path) -> None:
    import exomem

    if not Path(exomem.__file__).resolve().is_relative_to(product_root / "src"):
        raise SystemExit(f"imported exomem from {exomem.__file__}, not from {product_root}/src")
    from exomem.init import init_vault
    from exomem.kbdir import kb_dirname

    init_vault(vault)
    shutil.copytree(seed, vault / kb_dirname(), dirs_exist_ok=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]).resolve())
