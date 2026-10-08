"""Single-host/no-sync deployment custody for the collection store (design §16 A5).

Custody is derived from actual paths, never from a caller flag. Configured sync
roots can only narrow it: they are refusals the deployment owner declares, not
grants. A synced deployment needs a synced adapter that verifies exclusion at
every participating endpoint; until one exists its custody stays unknown, which
leaves store activation pending without touching file collections or knowledge.
Unmanaged sync programs that leave no evidence on these paths are outside the
supported deployment boundary.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .. import state_paths

SYNC_ROOTS_ENV = "EXOMEM_COLLECTION_STORE_SYNC_ROOTS"

# Metadata a sync client keeps inside the root it synchronizes. Per-user client
# configuration (for example a home-directory ``.dropbox`` directory) is not a root.
_CLIENT_ROOT_ENTRIES = (
    ".stfolder", ".dropbox.cache", ".sync/ID", ".tresorit", ".SynologyWorkingDirectory",
    ".nextcloudsync.log", ".owncloudsync.log",
)
_CLIENT_ROOT_FILES = (".dropbox",)
_CLIENT_ROOT_PREFIXES = ("._sync_", ".sync_")
_CLIENT_PATH_COMPONENTS = ("com~apple~CloudDocs", "Mobile Documents")
_PLATFORM_SYNC_ENV = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
# Windows sync folders keep no metadata in the tree, so on a Windows-mounted path their
# conventional names ("OneDrive - Contoso", "Dropbox (Personal)") are the evidence.
# nosemgrep: ep-word-set -- Provider folder-name fingerprints, not meaning; see the boundary above.
_WINDOWS_SYNC_FOLDERS = ("onedrive", "dropbox", "google drive", "iclouddrive")
_WINDOWS_FILESYSTEMS = frozenset({"9p", "drvfs"})
# Vault-application sync: Obsidian Sync and the community plugins that replicate files.
# nosemgrep: ep-word-set -- Exact plugin identifiers from community-plugins.json.
_SYNC_PLUGINS = frozenset({"obsidian-livesync", "remotely-save", "obsidian-git", "remotely-secure"})


@dataclass(frozen=True, slots=True)
class Custody:
    verified: bool
    reason: str


def _resolved(path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _configured_roots() -> list[Path]:
    roots = []
    for raw in os.environ.get(SYNC_ROOTS_ENV, "").split(os.pathsep):
        if raw.strip():
            if not Path(raw).is_absolute():
                raise ValueError(f"{SYNC_ROOTS_ENV} entries must be absolute paths")
            roots.append(_resolved(raw))
    roots.extend(_resolved(os.environ[name]) for name in _PLATFORM_SYNC_ENV
                 if os.environ.get(name, "").strip())
    return roots


def _client_evidence(path: Path) -> str | None:
    """Sync-client metadata at this path or any ancestor (the client's root)."""
    if any(part in _CLIENT_PATH_COMPONENTS for part in path.parts):
        return f"cloud document path {path}"
    for directory in (path, *path.parents):
        found = [name for name in _CLIENT_ROOT_ENTRIES if (directory / name).exists()]
        found += [name for name in _CLIENT_ROOT_FILES if (directory / name).is_file()]
        try:
            found += [name for name in os.listdir(directory)
                      if name.startswith(_CLIENT_ROOT_PREFIXES) and name.endswith(".db")]
        except OSError:
            pass
        if found:
            return f"sync client metadata {directory / found[0]}"
    return None


def _windows_mounts() -> list[Path] | None:
    """Windows drive mounts (WSL drvfs/9p); None means every path is a Windows path."""
    if os.name == "nt":
        return None
    try:
        with open("/proc/self/mounts", encoding="utf-8") as mounts:
            fields = [line.split() for line in mounts]
    except OSError:
        return []
    # nosemgrep: ep-lexical-intent -- /proc/self/mounts fstype tokens are kernel names.
    return [Path(entry[1].replace("\\040", " ")) for entry in fields
            if len(entry) > 2 and entry[2] in _WINDOWS_FILESYSTEMS]


def _windows_sync_folder(path: Path, mounts: list[Path] | None) -> str | None:
    if mounts is not None and not any(path.is_relative_to(mount) for mount in mounts):
        return None
    for part in path.parts:
        name = part.lower()
        if any(name == folder or name.startswith((folder + " ", folder + "-"))
               for folder in _WINDOWS_SYNC_FOLDERS):
            return f"Windows sync folder {part}"
    return None


def _vault_application_sync(vault: Path) -> str | None:
    config = vault / ".obsidian"

    def enabled(name):
        try:
            value = json.loads((config / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError) as error:
            raise ValueError(f"unreadable vault application config {name}") from error
        if isinstance(value, dict):
            return {key for key, on in value.items() if on is True}
        return set(value) if isinstance(value, list) else set()

    if "sync" in enabled("core-plugins.json"):
        return "vault application sync is enabled"
    found = enabled("community-plugins.json") & _SYNC_PLUGINS
    return f"vault sync plugin {sorted(found)[0]} is enabled" if found else None


def verify(vault_root) -> Custody:
    """Verify single-host/no-sync custody of the vault, its marker and the live store."""
    try:
        vault = _resolved(vault_root)
        state_root = _resolved(state_paths.state_store_root())
        store_directory = _resolved(state_paths.vault_state_dir(Path(vault_root)))
        if _overlap(vault, state_root) or _overlap(vault, store_directory):
            return Custody(False, "the live store and the vault overlap")
        for root in _configured_roots():
            if _overlap(root, vault) or _overlap(root, store_directory):
                return Custody(False, f"configured sync root {root} overlaps the vault or live store")
        mounts = _windows_mounts()
        for path in (vault, store_directory):
            evidence = _client_evidence(path) or _windows_sync_folder(path, mounts)
            if evidence is not None:
                return Custody(False, evidence)
        evidence = _vault_application_sync(vault)
        if evidence is not None:
            return Custody(False, evidence)
    except (OSError, ValueError) as error:
        return Custody(False, f"custody cannot be verified: {error}")
    return Custody(True, "single-host deployment without sync")


def verify_backup_destination(vault_root, destination) -> Custody:
    """A backup lands outside the vault and the live store's directory.

    Inside the vault a store copy is synced and backed up as vault content beside the
    one replica the vault may carry; in the live store's directory it could replace the
    live store or its WAL. A synced destination only warns (``backup_sync_warning``).
    """
    try:
        target = _resolved(destination)
        store_directory = _resolved(state_paths.vault_state_dir(Path(vault_root)))
        for root, what in ((_resolved(vault_root), "the vault"), (store_directory, "the live store's directory")):
            if target.is_relative_to(root):
                return Custody(False, f"the destination is inside {what}")
    except (OSError, ValueError) as error:
        return Custody(False, f"the destination cannot be verified: {error}")
    return Custody(True, "outside the vault and the live store")


def backup_sync_warning(destination) -> str | None:
    """Why a sync client may carry this backup and its scratch family, or None.

    Backing up into a synced folder is the owner's choice, so this never refuses.
    """
    try:
        target = _resolved(destination)
        for root in _configured_roots():
            if target.is_relative_to(root):
                return f"the destination is inside configured sync root {root}"
        return _client_evidence(target.parent) or _windows_sync_folder(target, _windows_mounts())
    except (OSError, ValueError) as error:
        return f"the destination's sync state cannot be read: {error}"
