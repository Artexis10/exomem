"""The stand-in's answer to the backup Job's vault check, in place of the
real image's exomem.vault._is_vault (manifests.py VAULT_CHECK_COMMAND). The
stand-in's cell-init writes this sentinel (exomem.py)."""

from pathlib import Path

SENTINEL = Path(".exomem") / "schema" / "SKILL.md"


def _is_vault(path: Path) -> bool:
    return (Path(path) / SENTINEL).exists()
