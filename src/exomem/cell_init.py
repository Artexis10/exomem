"""The `cell-init` entrypoint for Exomem Cloud cells (design D3).

Runs once, idempotently, ahead of the server (as a Kubernetes init container):

1. `/data/vault` and, when passed, `/data/host` exist at mode `0700`;
2. vault init, atomically, when the volume carries no vault yet;
3. offline state migration (`maintain --migrate-state --offline`).

That is the desktop's own first-run path, so the cell keeps the one rule:
there is no governance schema migration and no custody environment. A cell
runs standalone governance defaults, like a fresh desktop install. Schema v4
with standalone custody attachment is a non-public foundation for a later
change (vault consolidation), not something a cloud cell reaches on its own —
measured against this image, a vault taken to v4 either refuses every write
(`GOVERNANCE_CATALOG_PUBLICATION_BLOCKED`) or, with the hosted custody
variables set, refuses every recall (`governed projected retrieval is
unavailable`).

`init` is skipped once the volume holds a vault, and state migration is
idempotent, so a run interrupted at any point is completed cleanly by the
next run. Vault creation itself is atomic (build in a staging sibling, then
`os.rename` onto the vault path): `init_vault` binds a per-vault state key to
the path it is called with, so building directly at the final path and
crashing partway would leave `_is_vault()` false forever (a wedged cell,
`FileExistsError` on retry) or, past the point the sentinel file lands, a
retry that reports success over a truncated vault -- neither of which a
crash may produce.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import init as init_module
from . import state_migration
from .vault import _is_vault

#: Prefix for a staging sibling of the vault path (`/data/.vault-init-<hex>`,
#: design D3.2). Random, not deterministic, because a crashed staging
#: directory is never resumed -- only removed and rebuilt from scratch -- so
#: there is nothing for a fixed name to help two concurrent runs coordinate on.
_STAGING_PREFIX = ".vault-init-"


class CellInitError(RuntimeError):
    """Raised for a cell-init failure with a stable code and failing step."""

    def __init__(self, code: str, step: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.step = step


@dataclass(frozen=True, slots=True)
class CellInitResult:
    vault_created: bool


def account_home() -> Path:
    """The running account's passwd home: the cloud cell's `/data/host`.

    The same entry standalone custody resolves its host-control root from
    (`authorization_custody._standalone_host_control_root`), never `$HOME`,
    which a pod spec can set to anything.
    """
    try:
        import pwd

        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        raise CellInitError(
            "CELL_INIT_HOST_ROOT_UNAVAILABLE",
            "prepare_directories",
            "the running account has no passwd home",
        ) from None
    if not home.is_absolute():
        raise CellInitError(
            "CELL_INIT_HOST_ROOT_UNAVAILABLE",
            "prepare_directories",
            "the running account's passwd home is not absolute",
        )
    return home


def _ensure_private_directory(path: Path) -> None:
    """Create `path` if absent and enforce mode `0700`, every run.

    Idempotent enforcement, not just first creation: a Kubernetes `fsGroup`
    volume can leave a freshly mounted directory setgid at a looser mode
    (`2755`) than the owner-only mode D2 requires. The mode is set through a
    descriptor opened without following a final symlink, so a symlink planted
    at `path` is refused (`ELOOP`) and its target is never touched.
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def _staging_siblings(vault_root: Path) -> list[Path]:
    parent = vault_root.parent
    if not parent.is_dir():
        return []
    return sorted(
        candidate
        for candidate in parent.iterdir()
        if candidate.name.startswith(_STAGING_PREFIX) and candidate != vault_root
    )


def _remove_stale_staging(vault_root: Path) -> None:
    """Remove staging siblings from an earlier crashed run.

    A staging directory is never resumed, so any that survives from a prior
    attempt is stale by definition and safe to discard before building a new
    one (design D3.2).
    """
    for stale in _staging_siblings(vault_root):
        shutil.rmtree(stale, ignore_errors=True)


def _ensure_vault(vault_root: Path) -> bool:
    """Initialize the vault atomically when the volume is empty. Idempotent."""

    if _is_vault(vault_root):
        return False

    _remove_stale_staging(vault_root)

    if any(vault_root.iterdir()):
        # An interrupted init can never produce this: a crash before the
        # rename leaves `vault_root` exactly as `_ensure_private_directory`
        # made it (empty), and a crash after the rename makes `_is_vault`
        # true above. Non-empty and unrecognized means something else wrote
        # here, so it is never overlaid (design D3.2).
        raise CellInitError(
            "CELL_INIT_VAULT_UNRECOGNIZED",
            "vault_init",
            "the vault path is neither empty nor a recognized vault",
        )

    staging = vault_root.parent / f"{_STAGING_PREFIX}{secrets.token_hex(8)}"
    staging.mkdir(mode=0o700, parents=False)
    if os.name != "nt":
        # `mkdir(mode=...)` does not override the kernel's own setgid
        # inheritance: a directory created under a setgid parent (a
        # Kubernetes `fsGroup` volume root, D2/D3.3) comes out setgid
        # regardless of the requested mode, and every scaffold entry
        # `init_vault` creates under it would inherit the same bit in turn.
        # An explicit `chmod` after creation is the only way to actually
        # clear it, so it runs before the scaffold is built, not only after.
        os.chmod(staging, 0o700)
    try:
        # `initialize_state=False`: state migration binds a per-vault state
        # key to the path `init_vault` is called with, so this must run
        # before the rename, then `_ensure_state_migrated` runs it again,
        # unconditionally, against the real (post-rename) `vault_root`.
        # Building state at the staging path here would bind identity to a
        # path this run is about to delete.
        init_module.init_vault(staging, initialize_state=False)
        vault_root.rmdir()
        os.replace(staging, vault_root)
        if os.name != "nt":
            # The renamed-in directory carries whatever mode `staging` had;
            # re-assert 0700 so the final vault path's mode never depends on
            # staging's history.
            os.chmod(vault_root, 0o700)
    except (KeyboardInterrupt, SystemExit):
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except CellInitError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise CellInitError("CELL_INIT_VAULT_FAILED", "vault_init", str(error)) from error
    return True


def _ensure_state_migrated(vault_root: Path) -> None:
    try:
        authority = state_migration.assert_offline_migration_authority(
            source="exomem cell-init",
        )
        state_migration.migrate_vault_state_offline(vault_root, authority=authority)
    except CellInitError:
        raise
    except Exception as error:
        raise CellInitError(
            "CELL_INIT_STATE_MIGRATION_FAILED", "state_migration", str(error)
        ) from error


def run_cell_init(vault_root: Path, *, host_root: Path | None = None) -> CellInitResult:
    """Run the idempotent cell-init sequence against `vault_root`.

    `host_root`, when given, is enforced to mode `0700` alongside the vault
    path (design D3.1, `/data/host`). It is optional so a caller that has no
    stake in a host root -- every test here, and any future caller that only
    cares about the vault -- never touches one that was not asked for.
    """

    root = Path(vault_root)
    try:
        _ensure_private_directory(root)
        if host_root is not None:
            _ensure_private_directory(Path(host_root))
    except OSError as error:
        raise CellInitError(
            "CELL_INIT_DIRECTORY_FAILED", "prepare_directories", str(error)
        ) from error
    vault_created = _ensure_vault(root)
    _ensure_state_migrated(root)
    return CellInitResult(vault_created=vault_created)
